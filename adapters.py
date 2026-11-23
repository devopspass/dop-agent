"""Channel-adapter abstraction.

This module defines the minimal ``Adapter`` protocol plus the neutral model
around it (``Capabilities``, ``Button``, ``MessageRef``, ``IncomingMessage``,
``AdapterRegistry``, ``ConversationBroadcaster``, ``ThrottledEditor``), and
two built-in adapters: ``SlackAdapter`` (wraps every Slack Bolt call:
``chat_postMessage``, ``chat_update``, ``files_upload_v2``,
``chat_getPermalink``, reactions, ``chat_delete``) and ``WebAdapter`` (the Web
UI's own send/notify path). ``handlers.py`` and ``watchers.py`` reach the
platform only through an adapter, never via ``client.*``/``say()`` directly.
See README "Adapter extension guide" for adding a platform.

``WebAdapter.send``/``update`` are no-ops: writing to ``message_store`` is
the delivery, and its ``_notify_change`` -> registered change listeners ->
SSE push does the rest.

Design notes:
- ``send``/``update`` are pure transport; recording lives in
  ``deliver``/``deliver_update`` (one write per message).
- ``send``'s ``record`` flag (default ``True``) supports the one case where
  a Slack post must NOT also write a new ``message_store`` row: multi-chunk
  responses that exceed Slack's per-message length limit post several Slack
  messages for a single already-recorded logical reply (recorded once,
  under one ``message_id``, before the chunk loop runs). ``record=False``
  posts via a bare ``chat_postMessage`` with no persistence side effect.
- ``upload_file`` supports both the single-file shape (``data``/``filename``)
  and the multi-page shape (``file_uploads=[...]``, used when rendering a
  multi-page Markdown doc to several PNGs). ``wait_posted=True`` opts into
  polling ``files_info`` until Slack has actually registered the share
  message (see ``_wait_file_posted``) — needed only by the table-image
  render path, where a later text message could otherwise land with an
  earlier timestamp than the image it was meant to follow.
- ``add_reaction``/``remove_reaction``/``delete_message`` are thin
  passthroughs; callers keep their own non-fatal try/except around them.
- ``format_markdown`` wraps ``slack_formatting.md_to_slack`` +
  ``split_message`` — the two functions that encode Slack's own formatting
  and length-limit quirks (``SLACK_MAX_CHARS = 3900``, ``*bold*`` not
  ``**bold**``). A future non-Slack adapter would implement its own platform
  rules behind the same method name.
- ``permalink`` wraps ``client.chat_getPermalink`` and is best-effort:
  failures return None instead of raising.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

import message_store
from message_ref import MessageRef
from bot_config import SLACK_BLOCK_MAX_CHARS
from slack_formatting import SLACK_MAX_CHARS, md_to_slack, split_message

log = logging.getLogger("adapters")


def thread_key_for_binding(platform: str, external_key: str) -> dict[str, Any] | None:
    """Translate a conversation_bindings row back into an adapter thread_key.

    Delegates to ``Adapter.thread_key_from_binding``. Only the built-in
    platforms are known here; use an ``AdapterRegistry`` to resolve other
    adapters."""
    cls = {message_store.SLACK_PLATFORM: SlackAdapter, message_store.WEB_PLATFORM: WebAdapter}.get(platform)
    if cls is None:
        return None
    return cls.thread_key_from_binding(None, external_key)  # type: ignore[arg-type]


_RECORDING_KWARGS = frozenset({
    "conversation_id", "role", "store_text", "record", "message_id",
    "external_ref", "created_at", "updated_at",
})


def _transport_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop recording-only kwargs so they never reach a platform API."""
    return {k: v for k, v in kwargs.items() if k not in _RECORDING_KWARGS}


def _native_id(message_ref: Any) -> Any:
    return message_ref.id if isinstance(message_ref, MessageRef) else message_ref


def _record(adapter: Any, conv_id: str, role: str, content: str, *, ref: MessageRef,
            message_id: str | None, buttons: list[Button] | None, external_ref: str | None,
            created_at: str | None, updated_at: str | None) -> str | None:
    extras: dict[str, Any] = {}
    ext = external_ref or adapter.external_ref(ref)
    if ext:
        extras["external_ref"] = ext
    if created_at is not None:
        extras["created_at"] = created_at
    if updated_at is not None:
        extras["updated_at"] = updated_at
    if adapter.capabilities.store_backed and buttons is not None:
        shown = [b for b in buttons if not b.native_only]
        if shown or not buttons:
            extras["actions"] = [{"id": b.action_id, "label": b.label} for b in shown]
    if message_id is not None:
        extras["message_id"] = message_id
    store = getattr(adapter, "store", message_store)
    return store.record_message(conv_id, role, content, **extras)


async def deliver(
    adapter: Any, thread_key: dict[str, Any], text: str, *,
    conversation_id: str | None = None, role: str = "assistant",
    store_text: str | None = None, record: bool = True,
    buttons: list[Button] | None = None, footnote: str | None = None,
    external_ref: str | None = None, created_at: str | None = None,
    updated_at: str | None = None, **kwargs: Any,
) -> tuple[Any, str | None]:
    """Send via ``adapter`` (pure transport) and record exactly once.

    Returns ``(native_result, stored_message_id)``. Store-backed adapters
    (Web UI) always record - recording is their delivery - and need a
    conversation id; ``record=False`` only skips the write for platforms
    where the post and the store row are separate (e.g. continuation chunks
    of an already-recorded reply)."""
    caps = adapter.capabilities
    conv_id = conversation_id or (thread_key.get("conversation_id") if caps.store_backed else None)
    if caps.store_backed and not conv_id:
        return None, None
    result = await adapter.send(thread_key, text, buttons=buttons, footnote=footnote, **kwargs)
    if not conv_id or not (record or caps.store_backed):
        return result, None
    ref = adapter.message_ref(thread_key, result)
    content = store_text if store_text is not None else _with_footnote(text, footnote)
    message_id = _record(adapter, conv_id, role, content, ref=ref, message_id=None, buttons=buttons,
                         external_ref=external_ref, created_at=created_at, updated_at=updated_at)
    return result, message_id


async def deliver_update(
    adapter: Any, thread_key: dict[str, Any], message_ref: Any, text: str, *,
    conversation_id: str | None = None, role: str = "assistant",
    message_id: str | None = None, store_text: str | None = None,
    buttons: list[Button] | None = None, footnote: str | None = None,
    external_ref: str | None = None, created_at: str | None = None,
    updated_at: str | None = None, **kwargs: Any,
) -> tuple[Any, str | None]:
    """Edit a message via ``adapter`` (pure transport) and record exactly once."""
    caps = adapter.capabilities
    conv_id = conversation_id or (thread_key.get("conversation_id") if caps.store_backed else None)
    if caps.store_backed and not conv_id:
        return None, None
    ref = message_ref if isinstance(message_ref, MessageRef) else adapter.message_ref(
        thread_key, message_ref, message_id)
    result = await adapter.update(thread_key, ref, text, buttons=buttons, footnote=footnote, **kwargs)
    if not conv_id:
        return result, None
    content = store_text if store_text is not None else _with_footnote(text, footnote)
    stored = _record(adapter, conv_id, role, content, ref=ref, message_id=message_id, buttons=buttons,
                     external_ref=external_ref, created_at=created_at, updated_at=updated_at)
    return result, stored


class ThrottledEditor:
    """Coalesces rapid edits of a live status message to the adapter's own
    ``capabilities.edit_min_interval`` (platforms rate-limit message edits).

    ``edit()`` sends immediately when the interval has elapsed, otherwise it
    keeps only the LATEST text as pending and schedules a trailing send for
    when the interval expires, so the newest status always lands eventually.
    ``flush()`` sends the pending edit now; ``close()`` drops it and waits for
    any in-flight send - call it before a terminal edit (final answer,
    "interrupted") so a stale status can never land after, or instead of, the
    terminal text. With an interval of 0 (Web) every edit is sent
    immediately and synchronously. Edits go through
    ``deliver_update`` so each is still recorded exactly once. ``clock`` is
    injectable for tests; one asyncio loop only (lock-serialized sends)."""

    def __init__(self, adapter: Any, thread_key: dict[str, Any], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.adapter = adapter
        self.thread_key = thread_key
        self._clock = clock
        self.min_interval: float = adapter.capabilities.edit_min_interval
        self._last_sent: float | None = None
        self._pending: tuple[Any, str, dict[str, Any]] | None = None
        self._timer: "asyncio.Task | None" = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def edit(self, ref: Any, text: str, **kwargs: Any) -> None:
        if self._closed:
            return
        now = self._clock()
        if (self.min_interval <= 0 or self._last_sent is None
                or now - self._last_sent >= self.min_interval):
            self._pending = None
            await self._send(ref, text, kwargs)
            return
        self._pending = (ref, text, kwargs)
        if self._timer is None or self._timer.done():
            delay = max(self.min_interval - (now - self._last_sent), 0.0)
            self._timer = asyncio.ensure_future(self._trailing(delay))

    async def _trailing(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._timer = None
        try:
            await self.flush()
        except Exception as exc:  # trailing send has no caller to raise to
            log.debug("Throttled status edit failed: %s", exc)

    async def flush(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        pending, self._pending = self._pending, None
        if pending and not self._closed:
            await self._send(*pending)

    async def close(self) -> None:
        """Discard any pending edit and wait for an in-flight one."""
        self._closed = True
        self._pending = None
        timer, self._timer = self._timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        async with self._lock:
            pass

    async def _send(self, ref: Any, text: str, kwargs: dict[str, Any]) -> None:
        async with self._lock:
            self._last_sent = self._clock()
            await deliver_update(self.adapter, self.thread_key, ref, text, **kwargs)


class AdapterRegistry:
    """Platform name -> adapter lookup; the single place the core asks
    "which adapter serves this platform / conversation?" instead of inferring
    it from whether a Slack ``channel`` happens to be truthy.

    Accepts a list of adapters (keyed by their ``platform`` attribute) or a
    ``{platform: adapter}`` dict (``None`` values are skipped, which lets
    callers pass "Slack adapter if a client exists" without branching)."""

    def __init__(self, adapters: Any = ()) -> None:
        self._adapters: dict[str, Any] = {}
        if isinstance(adapters, dict):
            for platform, adapter in adapters.items():
                if adapter is not None:
                    self._adapters[platform] = adapter
        else:
            for adapter in adapters:
                self.register(adapter)

    def register(self, adapter: Any) -> None:
        self._adapters[adapter.platform] = adapter

    def unregister(self, platform: str) -> None:
        self._adapters.pop(platform, None)

    def get(self, platform: str) -> Any | None:
        return self._adapters.get(platform)

    def platforms(self) -> list[str]:
        return list(self._adapters)

    def slack_for(self, client: Any) -> Any:
        """The registered Slack adapter when it wraps ``client``; otherwise a
        transient (unregistered) ``SlackAdapter`` around it. Lets Bolt
        listeners, which receive their own ``client``, reach Slack through
        the registry without every call site constructing an adapter."""
        registered = self._adapters.get(message_store.SLACK_PLATFORM)
        if registered is not None and getattr(registered, "client", None) is client:
            return registered
        return SlackAdapter(client)

    def __contains__(self, platform: str) -> bool:
        return platform in self._adapters

    def external_target(self, conversation_id: str) -> tuple[Any, dict[str, Any]] | None:
        """(adapter, thread_key) of the conversation's first bound NON-Web
        platform that has a registered adapter, else None (Web-only)."""
        for binding in message_store.list_conversation_bindings(conversation_id):
            adapter = self._adapters.get(binding["platform"])
            if adapter is None or binding["platform"] == message_store.WEB_PLATFORM:
                continue
            thread_key = adapter.thread_key_from_binding(binding["external_key"])
            if thread_key is not None:
                return adapter, thread_key
        return None

    def new_conversation_target(self) -> tuple[Any, dict[str, Any]] | None:
        """Where to mirror a brand-new Web UI conversation: the first adapter
        that offers a ``new_conversation_thread_key()`` (e.g. a known DM)."""
        for adapter in self._adapters.values():
            offer = getattr(adapter, "new_conversation_thread_key", None)
            thread_key = offer() if offer else None
            if thread_key:
                return adapter, thread_key
        return None

    def delivery_for(self, conversation_id: str, *, strict: bool = False) -> tuple[Any, dict[str, Any]] | None:
        """Where should an out-of-band message (watch fire, system notice)
        for this conversation be delivered?

        The first bound external platform (anything but the Web UI, in
        binding-creation order) that has a registered adapter wins, because
        that is where the user is actually reading; otherwise the Web UI
        adapter delivers straight into the conversation. Returns ``None``
        only if no suitable adapter is registered at all.

        ``strict=True`` refuses the Web fallback when the conversation *is*
        bound to an external platform whose adapter simply isn't registered
        in this process (returns ``None``): callers that must not silently
        divert a message away from where the user reads it (announcements)
        use this and retry later."""
        unreachable_binding = False
        for binding in message_store.list_conversation_bindings(conversation_id):
            platform = binding["platform"]
            if platform == message_store.WEB_PLATFORM:
                continue
            adapter = self._adapters.get(platform)
            if adapter is None:
                unreachable_binding = True
                continue
            thread_key = adapter.thread_key_from_binding(binding["external_key"])
            if thread_key is not None:
                return adapter, thread_key
        if strict and unreachable_binding:
            return None
        web = self._adapters.get(message_store.WEB_PLATFORM)
        if web is None:
            return None
        return web, {"conversation_id": conversation_id}


@dataclass(frozen=True)
class IncomingMessage:
    """Platform-neutral inbound message: what any adapter's listener (Bolt,
    Telegram long-poll, ...) hands to ``handlers.handle_incoming``.

    ``conversation_key`` is the adapter's external binding key (see
    ``Adapter.external_key``); ``ref`` is the platform's message identity
    string (``MessageRef.key()``) used to dedup redelivered updates."""

    platform: str
    user_id: str
    conversation_key: str
    text: str
    attachments: tuple = ()
    ref: str | None = None


@dataclass(frozen=True)
class BroadcastOrigin:
    """Origin tag for a logical event, used to avoid re-delivering it back
    to the same adapter/binding that created it."""

    platform: str
    external_key: str


class ConversationBroadcaster:
    """Best-effort fan-out of one logical message/event to every bound adapter
    except the exact origin binding that created it.

    The shared SQLite message store is already the Web UI's source of truth,
    so callers choose when a given adapter should or should not record. For
    example, a Web-origin user message is first recorded once via WebAdapter,
    then mirrored to Slack with ``record=False`` to avoid a duplicate DB row.
    """

    def __init__(self, adapters: "AdapterRegistry | dict[str, Adapter]") -> None:
        self._registry = adapters if isinstance(adapters, AdapterRegistry) else AdapterRegistry(adapters)

    @property
    def registry(self) -> "AdapterRegistry":
        return self._registry

    async def fanout_send(
        self,
        conversation_id: str,
        text: str,
        *,
        role: str,
        origin: BroadcastOrigin | None = None,
        store_text: str | None = None,
        record_by_platform: dict[str, bool] | None = None,
        text_by_platform: dict[str, str] | None = None,
        explicit_thread_keys: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, tuple[Any, str | None]]:
        deliveries: dict[str, tuple[Any, str | None]] = {}
        targets: dict[tuple[str, str], dict[str, Any]] = {}
        for binding in message_store.list_conversation_bindings(conversation_id):
            platform = binding["platform"]
            adapter = self._registry.get(platform)
            if adapter is None:
                continue
            thread_key = adapter.thread_key_from_binding(binding["external_key"])
            if thread_key is not None:
                targets[(platform, binding["external_key"])] = thread_key
        for platform, thread_key in (explicit_thread_keys or {}).items():
            adapter = self._registry.get(platform)
            external_key = adapter.external_key(thread_key) if adapter is not None else ""
            if external_key:
                targets.setdefault((platform, external_key), thread_key)

        for (platform, external_key), thread_key in targets.items():
            if origin and platform == origin.platform and external_key == origin.external_key:
                continue
            adapter = self._registry.get(platform)
            if adapter is None:
                continue
            deliveries[platform] = await deliver(
                adapter,
                thread_key,
                (text_by_platform or {}).get(platform, text),
                conversation_id=conversation_id,
                role=role,
                store_text=store_text,
                record=(record_by_platform or {}).get(platform, True),
                **kwargs,
            )
        return deliveries


def thread_key_after_post(adapter: Any, thread_key: dict[str, Any], ref_id: str) -> dict[str, Any]:
    """thread_key to bind after a top-level post: adapters whose post starts
    a thread (Slack) say so via ``thread_key_after_post``; others keep it."""
    hook = getattr(adapter, "thread_key_after_post", None)
    return hook(thread_key, ref_id) if hook else thread_key


@dataclass(frozen=True)
class Button:
    """Platform-neutral interactive button attached to a message.

    ``buttons=None`` on ``send``/``update`` means "plain text, leave any
    existing controls alone"; ``buttons=[]`` means "rich message with no
    controls" (i.e. strip buttons from an earlier version of the message);
    a non-empty list renders each platform's native control (Slack Block Kit
    button, Web UI action row, ignored where ``supports_buttons`` is False).
    ``style`` is ``"danger"``/``"primary"``/None. ``native_only`` marks a
    control that only the chat platform's own UI can service (the Web UI
    has its own Stop control in the live bubble), so store-backed adapters
    skip it.
    """

    label: str
    action_id: str
    value: str | None = None
    style: str | None = None
    native_only: bool = False


def _with_footnote(text: str, footnote: str | None) -> str:
    return f"{text}\n\n{footnote}" if footnote else text


@dataclass(frozen=True)
class Capabilities:
    """What a platform can do, so the core checks a flag instead of calling
    an op and catching ``NotImplementedError``.

    ``max_chars``: per-message text limit (None = unlimited).
    ``max_rich_chars``: limit for text carried in a rich/button message
    (None = same as ``max_chars``).
    ``store_backed``: the transport IS the message store (Web UI), so the
    final reply is already visible once recorded and no post-turn delivery
    step is needed.
    ``edit_min_interval``: minimum seconds between edits of one message
    (platform flood control); 0 = no throttling needed."""

    max_chars: int | None = None
    max_rich_chars: int | None = None
    supports_edit: bool = True
    supports_reactions: bool = False
    supports_uploads: bool = False
    supports_delete: bool = False
    supports_buttons: bool = False
    edit_min_interval: float = 0.0
    store_backed: bool = False


@runtime_checkable
class Adapter(Protocol):
    """Minimal surface a chat-platform integration must implement.

    A ``thread_key`` is whatever the platform natively addresses a
    conversation with (for Slack: ``channel`` + optional ``thread_ts``,
    passed as a small dict rather than positional args, since platforms
    need different fields).

    ``platform`` is the adapter's registry key (it matches
    ``conversation_bindings.platform``). ``thread_key_from_binding`` /
    ``external_key`` translate between a binding's opaque ``external_key``
    string and the adapter's native ``thread_key`` — the core never parses
    either itself.
    """

    platform: str
    capabilities: Capabilities

    def thread_key_from_binding(self, external_key: str) -> dict[str, Any] | None:
        """Binding ``external_key`` -> thread_key, or None if unusable."""
        ...

    def external_key(self, thread_key: dict[str, Any]) -> str:
        """thread_key -> the binding ``external_key`` string ('' if none)."""
        ...

    def message_ref(self, thread_key: dict[str, Any], native: Any, message_id: str | None = None) -> MessageRef:
        """Neutral id of a message from a ``send()`` result (or a native id)."""
        ...

    def external_ref(self, ref: MessageRef) -> str | None:
        """Value stored in ``messages.external_ref`` for dedup/lookup (or None)."""
        ...

    async def send(
        self,
        thread_key: dict[str, Any],
        text: str,
        *,
        buttons: list[Button] | None = None,
        footnote: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Pure transport: post a new message and return the platform's
        native result. Never writes ``message_store`` - use ``deliver()``,
        the single place that records.

        ``buttons``/``footnote`` are the neutral rich-message model (see
        ``Button``): the adapter renders them natively or ignores them when
        ``capabilities.supports_buttons`` is False. Callers pass plain
        Markdown-ish text and never platform wire formats."""
        ...

    async def update(
        self,
        thread_key: dict[str, Any],
        message_ref: Any,
        text: str,
        *,
        buttons: list[Button] | None = None,
        footnote: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Pure transport: edit an already-sent message in place
        (``message_ref``: a ``MessageRef`` or the native id)."""
        ...

    async def upload_file(
        self,
        thread_key: dict[str, Any],
        data: bytes | None = None,
        filename: str | None = None,
        *,
        file_uploads: list[dict[str, Any]] | None = None,
        wait_posted: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Upload/attach a file (image, PDF, etc.) to the thread. Either a
        single file (``data``/``filename``) or a multi-file batch
        (``file_uploads``, platform-native shape) may be given."""
        ...

    async def add_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        """Add an emoji reaction to a specific message (``ts``)."""
        ...

    async def remove_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        """Remove an emoji reaction from a specific message (``ts``)."""
        ...

    async def delete_message(self, thread_key: dict[str, Any], ts: str) -> Any:
        """Delete a previously sent message (``ts``)."""
        ...

    def format_markdown(self, text: str) -> list[str]:
        """Convert generic Markdown into this platform's native text
        format, pre-split into chunks that respect the platform's own
        per-message length limit."""
        ...

    async def permalink(self, thread_key: dict[str, Any]) -> str | None:
        """Best-effort permalink to the thread root, or None if unavailable
        (never raises)."""
        ...


class SlackAdapter:
    """Slack transport over a Slack Bolt/WebClient ``client``.

    ``thread_key`` is ``{"channel": ..., "thread_ts": ...}``.
    """

    platform = message_store.SLACK_PLATFORM
    capabilities = Capabilities(
        max_chars=SLACK_MAX_CHARS, max_rich_chars=SLACK_BLOCK_MAX_CHARS, supports_edit=True, supports_reactions=True,
        supports_uploads=True, supports_delete=True, supports_buttons=True,
        edit_min_interval=1.0,
    )

    def __init__(self, client) -> None:
        self._client = client

    def new_conversation_thread_key(self) -> dict[str, Any] | None:
        """Where a brand-new Web UI conversation can be mirrored on this
        platform (the known DM channel, no thread yet), else None."""
        channel = message_store.find_known_dm_channel()
        return {"channel": channel, "thread_ts": None} if channel else None

    def thread_key_after_post(self, thread_key: dict[str, Any], ref_id: str) -> dict[str, Any]:
        """A top-level post becomes its own thread root."""
        if thread_key.get("thread_ts"):
            return thread_key
        return {**thread_key, "thread_ts": ref_id}

    @property
    def client(self):
        """The underlying Slack client (for Slack-only helpers such as
        ``_session_context``); core code reaches it via the adapter instead
        of threading a separate ``client`` argument around."""
        return self._client

    def thread_key_from_binding(self, external_key: str) -> dict[str, Any] | None:
        channel, _sep, thread_ts = external_key.partition(":")
        if not channel:
            return None
        return {"channel": channel, "thread_ts": thread_ts or None}

    def external_key(self, thread_key: dict[str, Any]) -> str:
        channel = thread_key.get("channel", "")
        thread_ts = thread_key.get("thread_ts")
        return f"{channel}:{thread_ts}" if thread_ts else channel

    @staticmethod
    def _channel_ts(thread_key: dict[str, Any]) -> tuple[str, str | None]:
        return thread_key["channel"], thread_key.get("thread_ts")

    @staticmethod
    def _render_rich(text: str, buttons: list[Button] | None, footnote: str | None,
                     kwargs: dict[str, Any]) -> str:
        """Translate the neutral message model into Slack's wire format.

        Returns the (fallback) ``text`` and mutates ``kwargs`` with
        ``mrkdwn`` and, for rich messages (``buttons``/``footnote`` given),
        Block Kit ``blocks``: a section (truncated to Slack's 3000-char
        section cap), an optional context footnote and an optional actions
        row. Legacy ``actions`` (Web UI's concept) is dropped.
        """
        kwargs.pop("actions", None)
        kwargs.setdefault("mrkdwn", True)
        if buttons is None and footnote is None:
            return text
        section_text = text
        if len(section_text) > SLACK_BLOCK_MAX_CHARS:
            section_text = section_text[: SLACK_BLOCK_MAX_CHARS - 1] + "…"
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": section_text}}
        ]
        if footnote:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footnote}]})
        if buttons:
            elements = []
            for b in buttons:
                el: dict[str, Any] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": b.label, "emoji": True},
                    "action_id": b.action_id,
                }
                if b.value is not None:
                    el["value"] = b.value
                if b.style:
                    el["style"] = b.style
                elements.append(el)
            blocks.append({"type": "actions", "elements": elements})
        kwargs["blocks"] = blocks
        return _with_footnote(text, footnote)

    @staticmethod
    def action_message_text(body: dict[str, Any], default: str) -> str:
        """Text of the first section of the message a Block Kit action was
        clicked on (``body`` is Slack's interaction payload), else ``default``."""
        try:
            existing = body["message"].get("blocks") or []
            if existing and existing[0].get("type") == "section":
                return existing[0]["text"]["text"]
        except Exception:
            pass
        return default

    @staticmethod
    def message_ref(thread_key: dict[str, Any], native: Any, message_id: str | None = None) -> MessageRef:
        ts = native.get("ts") if hasattr(native, "get") else native
        return MessageRef(message_store.SLACK_PLATFORM, thread_key.get("channel", ""), str(ts or ""))

    @staticmethod
    def external_ref(ref: MessageRef) -> str | None:
        if not ref.conversation_key or not ref.id:
            return None
        return message_store.slack_external_ref(ref.conversation_key, ref.id)

    async def send(
        self,
        thread_key: dict[str, Any],
        text: str,
        *,
        buttons: list[Button] | None = None,
        footnote: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Pure transport: ``chat_postMessage``; recording is ``deliver()``'s job."""
        kwargs = _transport_kwargs(kwargs)
        text = self._render_rich(text, buttons, footnote, kwargs)
        channel, thread_ts = self._channel_ts(thread_key)
        if thread_ts is not None:
            kwargs.setdefault("thread_ts", thread_ts)
        return await self._client.chat_postMessage(channel=channel, text=text, **kwargs)

    async def update(
        self,
        thread_key: dict[str, Any],
        message_ref: Any,
        text: str,
        *,
        buttons: list[Button] | None = None,
        footnote: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Pure transport: ``chat_update``; recording is ``deliver_update()``'s job."""
        kwargs = _transport_kwargs(kwargs)
        text = self._render_rich(text, buttons, footnote, kwargs)
        channel, _ = self._channel_ts(thread_key)
        return await self._client.chat_update(
            channel=channel, ts=_native_id(message_ref), text=text, **kwargs,
        )

    async def upload_file(
        self,
        thread_key: dict[str, Any],
        data: bytes | None = None,
        filename: str | None = None,
        *,
        file_uploads: list[dict[str, Any]] | None = None,
        wait_posted: bool = False,
        timeout: float = 10.0,
        poll_interval: float = 0.4,
        **kwargs: Any,
    ) -> Any:
        channel, thread_ts = self._channel_ts(thread_key)
        if thread_ts is not None:
            kwargs.setdefault("thread_ts", thread_ts)
        if file_uploads is not None:
            return await self._client.files_upload_v2(
                channel=channel, file_uploads=file_uploads, **kwargs,
            )
        resp = await self._client.files_upload_v2(
            channel=channel,
            file=data,
            filename=filename,
            **kwargs,
        )
        if wait_posted:
            await self._wait_file_posted(
                resp, channel=channel, filename=filename or "",
                timeout=timeout, poll_interval=poll_interval,
            )
        return resp

    async def _wait_file_posted(
        self, resp: Any, *, channel: str, filename: str, timeout: float, poll_interval: float,
    ) -> None:
        """Block until Slack has actually finished registering the
        file-share message (thumbnail generation, indexing) or ``timeout``
        elapses — see ``files.completeUploadExternal``'s async-completion
        gotcha. Never raises; best-effort ordering guarantee only."""
        file_id = None
        try:
            file_id = (resp.get("file") or {}).get("id")
        except Exception:
            file_id = None
        if not file_id:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = await self._client.files_info(file=file_id)
                shares = (info.get("file") or {}).get("shares") or {}
                if any(channel in shares.get(scope, {}) for scope in ("public", "private")):
                    return
            except Exception as exc:
                log.debug("files_info poll failed while waiting for upload of %s: %s", filename, exc)
                return
            await asyncio.sleep(poll_interval)
        log.debug("Timed out waiting for file share to register for %s", filename)

    async def add_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        channel, _ = self._channel_ts(thread_key)
        return await self._client.reactions_add(channel=channel, name=name, timestamp=_native_id(ts))

    async def remove_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        channel, _ = self._channel_ts(thread_key)
        return await self._client.reactions_remove(channel=channel, name=name, timestamp=_native_id(ts))

    async def delete_message(self, thread_key: dict[str, Any], ts: str) -> Any:
        channel, _ = self._channel_ts(thread_key)
        return await self._client.chat_delete(channel=channel, ts=_native_id(ts))

    def format_markdown(self, text: str) -> list[str]:
        return split_message(md_to_slack(text))

    async def permalink(self, thread_key: dict[str, Any]) -> str | None:
        channel, thread_ts = self._channel_ts(thread_key)
        if not thread_ts:
            return None
        try:
            resp = await self._client.chat_getPermalink(channel=channel, message_ts=thread_ts)
            return resp.get("permalink") if resp else None
        except Exception as exc:  # noqa: BLE001 - best-effort, matches existing call sites
            log.debug("[%s] chat_getPermalink failed (non-fatal): %s", channel, exc)
            return None


class WebAdapter:
    """Wraps the Web UI's own send/notify path: writing to
    ``message_store`` (the single source of truth the Web UI reads to
    render conversations) and letting its built-in change-notification
    mechanism (``_notify_change`` -> registered listeners) push the SSE
    update. Unlike ``SlackAdapter`` there is no separate remote-platform
    call to make here — a message is "sent" the moment it's recorded,
    since every open browser tab is just reading the same SQLite log.

    ``thread_key`` for this adapter is ``{"conversation_id": "..."}``.
    """

    platform = message_store.WEB_PLATFORM
    capabilities = Capabilities(max_chars=None, supports_edit=True, supports_buttons=True, store_backed=True)

    def __init__(self, *, store_module: Any = None) -> None:
        self._store = store_module or message_store

    def thread_key_from_binding(self, external_key: str) -> dict[str, Any] | None:
        return {"conversation_id": external_key} if external_key else None

    def external_key(self, thread_key: dict[str, Any]) -> str:
        return str(thread_key.get("conversation_id") or "")

    @staticmethod
    def _conversation_id(thread_key: dict[str, Any], conversation_id: str | None) -> str | None:
        return conversation_id or thread_key.get("conversation_id")

    @property
    def store(self):
        return self._store

    @staticmethod
    def message_ref(thread_key: dict[str, Any], native: Any, message_id: str | None = None) -> MessageRef:
        return MessageRef(
            message_store.WEB_PLATFORM, thread_key.get("conversation_id") or "",
            str(message_id or (native if isinstance(native, str) else "") or ""),
        )

    @staticmethod
    def external_ref(ref: MessageRef) -> str | None:
        return None

    async def send(self, thread_key: dict[str, Any], text: str, **kwargs: Any) -> Any:
        """No-op transport: the Web UI reads ``message_store``, so recording
        in ``deliver()`` IS the delivery."""
        return None

    async def update(self, thread_key: dict[str, Any], message_ref: Any, text: str, **kwargs: Any) -> Any:
        return None

    async def upload_file(
        self,
        thread_key: dict[str, Any],
        data: bytes | None = None,
        filename: str | None = None,
        *,
        file_uploads: list[dict[str, Any]] | None = None,
        wait_posted: bool = False,
        **kwargs: Any,
    ) -> Any:
        # Web UI has no file-attachment path; fail loudly rather than no-op.
        raise NotImplementedError("WebAdapter does not support upload_file")

    async def add_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        raise NotImplementedError("WebAdapter does not support add_reaction")

    async def remove_reaction(self, thread_key: dict[str, Any], name: str, ts: str) -> Any:
        raise NotImplementedError("WebAdapter does not support remove_reaction")

    async def delete_message(self, thread_key: dict[str, Any], ts: str) -> Any:
        raise NotImplementedError("WebAdapter does not support delete_message")

    def format_markdown(self, text: str) -> list[str]:
        # The Web UI renders raw Markdown client-side and has no length limit.
        return [text]

    async def permalink(self, thread_key: dict[str, Any]) -> str | None:
        return None


# ---------------------------------------------------------------------------
# Process-wide registry + sync->async bridge
# ---------------------------------------------------------------------------

#: The Web UI adapter is always available; platform adapters that need a live
#: client (Slack) are registered at startup by whoever owns the client
#: (``watchers.configure`` / ``web_server.init``). Tests register extra
#: adapters (e.g. a fake platform) here and unregister them afterwards.
REGISTRY = AdapterRegistry([WebAdapter()])

_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Remember the bot's event loop so synchronous callers on other threads
    (e.g. an MCP tool registering a watch) can run adapter coroutines on it."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def run_sync(coro: Any, *, timeout: float = 10.0) -> Any:
    """Run an adapter coroutine from synchronous code and return its result.

    - Called from a thread *other than* the bot loop's: schedules onto the
      bot loop and waits up to ``timeout`` (the adapter's HTTP client is
      bound to that loop, so it must not run anywhere else).
    - Called from the bot loop's own thread: blocking would deadlock, so it
      raises ``RuntimeError`` (callers treat that like any other delivery
      failure and fall back to their async retry path).
    - No bot loop at all (plain scripts/tests): runs it with ``asyncio.run``.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        coro.close()
        raise RuntimeError("run_sync() called from inside a running event loop")
    loop = _MAIN_LOOP
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
    return asyncio.run(coro)
