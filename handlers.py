"""
Slack message-handling logic — event handlers (DM/mention/actions), the
core turn-processing engine (``_handle``), and voice-message transcription
glue.

All ``@app.action``/``@app.event`` decorators live inside ``register(app)``,
called once from dop_agent.py right after the Slack ``AsyncApp`` is
constructed. This avoids a circular import: this module cannot import
``app`` from dop_agent.py, which imports ``_handle``, ``is_authorized`` and
``_transcribe_and_finalize`` from here (same late-binding pattern as
``watchers.configure(app, ...)``).

Gotchas:
  - Interrupting a turn must only call ``session.abort()`` and return the
    ``INTERRUPTED`` sentinel; never discard the saved Copilot session id on
    interrupt/abort, or the thread's history is lost.
  - ``_session_context`` (self-context injected into the first message of a
    new session: always the conversation_id, plus Slack workspace/channel/
    thread detail when Slack-bound) and the ``🧵 New session: <uuid>``
    first-reply announcement are deliberate — preserve them in ``_handle()``.
  - New stdlib usage (e.g. ``asyncio.sleep``) needs its own import; pyflakes
    doesn't catch every case, so cover code paths with tests.
  - Every Slack-client call (``chat_postMessage``/``chat_update``,
    ``files_upload_v2``, ``reactions_add/remove``, ``chat_delete``) goes
    through ``adapters.SlackAdapter``; ``_handle()`` and its helpers never
    call ``client.*``/``say()`` directly.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
import time
import uuid
from pathlib import Path

import adapters
import message_store
from message_ref import first_sighting
from adapters import Button, IncomingMessage, SlackAdapter, WebAdapter
from bot_config import (
    ALLOWED_USER_ID,
    AUDIO_AUTO_SEND,
    COPILOT_WORKDIR,
    INTERRUPTED,
    SLACK_BOT_TOKEN,
    TABLE_RENDER_MODE,
    log,
)
from copilot_session import (
    _conversation_id_for,
    _convos,
    _is_duplicate_event,
    _is_new_conversation,
    _thread_root,
    get_convo,
)
from persistence import (
    _clear_active_status,
    _clear_pending_transcription,
    _message_id_for_slack_message,
    _record_visible_message,
    _register_active_status,
    _register_pending_transcription,
)
from render_markdown import render_markdown_doc_png
from render_tables import extract_and_render_tables
from transcription import _is_audio_file, transcribe_slack_audio
import transcription
import audio_store

# conversation_id (per-thread) -> ordered list of {"ts": <msg ts>, "text": <transcript>}
# for voice messages transcribed but not yet sent to Copilot. Lets
# consecutive voice messages within the same thread be merged into a single
# combined text shown on the latest message.
_channel_audio_buffer: dict[str, list[dict]] = {}
# Audio-store files with a transcription running in this process (avoid double-starting on resume).
_inflight_audio: set[str] = set()

# Per-message-ts override of the default AUDIO_AUTO_SEND behavior, armed by
# clicking the placeholder's button (":eye: Preview before sending" or
# ":zap: Auto-send when ready", whichever is the opposite of the current
# default) *before* transcription finishes. Values are "auto" or "manual".
# Checked (and popped) once transcription completes to decide whether to
# forward the result to Copilot automatically or show the manual button.
_transcript_mode_override: dict[str, str] = {}


async def _session_context(client, channel: str, thread_ts: str | None, ts: str,
                            conversation_id: str, *, platform: str | None = None,
                            thread_key: dict | None = None) -> str:
    """Best-effort description of this Copilot session's own identity,
    prepended to the first message of every brand-new session (Slack-bound
    or Web-UI-only alike) so the agent knows who it is without extra tool
    calls. Always includes the bot-side ``conversation_id`` — the single
    global in-process MCP server (see web_server.py) cannot tell which
    conversation invoked a tool, so this is the only way an agent learns the
    value to pass to ``watch_add``/``watch_list``/``watch_pause_session``/
    ``session_get``. When running inside a Slack thread, also includes the
    workspace/channel/thread detail.

    Never raises — any Slack API failure (missing scope, rate limit, etc.)
    just means that piece of context is omitted, not that the turn fails.
    """
    if platform == message_store.TELEGRAM_PLATFORM:
        tk = thread_key or {}
        where = f"chat {tk.get('chat_id')}" + (f", forum topic {tk['topic_id']}" if tk.get("topic_id") else " (direct message)")
        return (
            "[Session context: this DOP Agent session is running inside a Telegram conversation]\n"
            f"- conversation_id: {conversation_id} (pass this exact value to watch_add/"
            "watch_list/watch_pause_session and session_get — there is no "
            "other way to know which conversation is calling those tools)\n"
            f"- Telegram {where}"
        )

    if not channel:
        return (
            "[Session context: this is a Web UI-only DOP Agent session, with no Slack thread]\n"
            f"- conversation_id: {conversation_id} (pass this exact value to watch_add/"
            "watch_list/watch_pause_session and session_get — there is no "
            "other way to know which conversation is calling those tools)"
        )

    root_ts = thread_ts or ts
    lines = [
        "[Session context: this DOP Agent session is running inside a Slack thread]",
        f"- conversation_id: {conversation_id} (pass this exact value to watch_add/"
        "watch_list/watch_pause_session and session_get — there is no "
        "other way to know which conversation is calling those tools)",
    ]

    try:
        info = await client.conversations_info(channel=channel)
        ch = info.get("channel", {})
        if ch.get("is_im"):
            lines.append(f"- Type: direct message (channel id: {channel})")
        else:
            name = ch.get("name")
            kind = "private channel" if ch.get("is_private") else "channel"
            lines.append(f"- Type: {kind} #{name} (channel id: {channel})" if name
                         else f"- Type: {kind} (channel id: {channel})")
    except Exception as exc:
        log.debug("[%s] conversations_info failed (non-fatal): %s", channel, exc)
        lines.append(f"- Channel id: {channel}")

    try:
        auth = await client.auth_test()
        team = auth.get("url") or auth.get("team")
        if team:
            lines.append(f"- Workspace: {team}")
    except Exception as exc:
        log.debug("[%s] auth_test failed (non-fatal): %s", channel, exc)

    try:
        link = await adapters.REGISTRY.slack_for(client).permalink({"channel": channel, "thread_ts": root_ts})
        if link:
            lines.append(f"- Thread permalink: {link}")
    except Exception as exc:
        log.debug("[%s] chat_getPermalink failed (non-fatal): %s", channel, exc)

    lines.append(f"- Thread root ts: {root_ts}")
    return "\n".join(lines)


async def maybe_transcribe_audio(event: dict, say, client, thread_ts: str | None = None,
                                  conversation_id: str | None = None,
                                  user_id: str | None = None) -> bool:
    """Slack entry point: if the Slack event carries one or more audio
    files, transcribe each via the platform-agnostic ``_transcribe_one()``
    core below (adapter=``SlackAdapter``) and post the result back.

    Returns True if it handled (any) audio files, False if there were none
    (caller should fall through to normal text handling).

    See ``_transcribe_one()`` for the full behavior description (buffering,
    auto-send vs. preview, etc.) — this function only does the Slack-specific
    bit: pulling audio files out of the raw Slack ``event`` dict and
    saving each downloaded file to ``audio_store`` before the placeholder is
    posted. Crash-resume (Slack and Web alike) is driven by the audio-store
    sidecar via ``resume_audio_jobs``.
    """
    files = event.get("files") or []
    audio_files = [f for f in files if _is_audio_file(f)]
    if not audio_files:
        return False

    channel = event.get("channel", "")
    thread_key = {"channel": channel, "thread_ts": thread_ts}
    adapter = adapters.REGISTRY.slack_for(client)
    buf_key = conversation_id or channel

    for f in audio_files:
        name = f.get("name") or "audio"
        try:
            # Persist the recording BEFORE anything else (placeholder, Whisper).
            audio_path = await transcription.save_slack_audio(
                f, conversation_id or channel, user_id=user_id, conversation_ref=conversation_id)
        except Exception as exc:
            log.exception("Could not download/save Slack audio %s", name)
            await adapters.deliver(adapter, thread_key, f":x: Could not fetch audio: {exc}",
                                   conversation_id=conversation_id, role="assistant")
            continue

        await _transcribe_one(
            name=name,
            transcribe=None,
            adapter=adapter,
            thread_key=thread_key,
            conversation_id=conversation_id,
            user_id=user_id,
            buf_key=buf_key,
            pending_registration=None,  # the audio_path sidecar tracks the job
            audio_path=audio_path,
        )

    return True


# Images from Slack "file_share" events (see maybe_capture_images) are cached
# locally so the Web UI can render an inline preview (web_server.py's
# attachment route) and the Copilot session can be told the saved path.
IMAGE_CACHE_DIR = Path.home() / ".copilot" / "dop-agent-files"

# Hard cap on a single image's size, checked against Slack's *declared*
# file size BEFORE downloading (not only after) - an oversized "image"
# must not even start a download, let alone occupy disk.
MAX_IMAGE_BYTES = 20 * 1024 * 1024


IMAGE_FILETYPES = {"png", "jpg", "jpeg", "gif", "webp"}


def _is_image_file(f: dict) -> bool:
    # Same check as transcription._is_audio_file(): fall back to filetype
    # when Slack's mimetype is missing or wrong.
    if (f.get("mimetype") or "").lower().startswith("image/"):
        return True
    return (f.get("filetype") or "").lower() in IMAGE_FILETYPES


def _sniff_image_mimetype(data: bytes) -> str | None:
    """Return the real mimetype based on the file's own magic bytes, never
    trusting Slack's client-supplied `mimetype` field at face value - a
    non-image could otherwise claim to be `image/png` and later get served
    back to a browser with that Content-Type by the attachment route."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def maybe_capture_images(event: dict, *, conversation_id: str | None) -> tuple[list[dict], list[dict]]:
    """If the Slack event carries one or more image files, download each
    (bot-token-authenticated GET via ``transcription._download_slack_file``,
    run in a thread executor) and cache it locally under
    ``IMAGE_CACHE_DIR/<conversation_id>/``.

    Returns ``(captured, skipped)``:
      - ``captured``: one dict per successfully cached image -
        ``{"local_path", "slack_file_id", "filename", "mimetype", "size"}``.
      - ``skipped``: one dict per file that failed size-cap, download, or
        magic-byte sniffing - ``{"filename", "reason"}`` - so the caller can
        post a visible status note instead of silently dropping it.

    Does NOT record anything in bot_store.db itself - the caller (handle_dm/
    handle_mention) does that once it knows the owning message_id, since
    that id doesn't exist until the user's message is recorded.
    """
    files = event.get("files") or []
    image_files = [f for f in files if _is_image_file(f)]
    if not image_files:
        return [], []

    loop = asyncio.get_running_loop()
    captured: list[dict] = []
    skipped: list[dict] = []
    conv_dir = IMAGE_CACHE_DIR / (conversation_id or "unbound")

    for f in image_files:
        name = f.get("name") or "image"
        declared_size = f.get("size") or 0
        if declared_size and declared_size > MAX_IMAGE_BYTES:
            log.warning("Skipping oversized image %s (%s bytes, max %s)", name, declared_size, MAX_IMAGE_BYTES)
            skipped.append({"filename": name, "reason": f"too large ({declared_size} bytes, max {MAX_IMAGE_BYTES})"})
            continue

        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            log.warning("Skipping image %s: no url_private to download from", name)
            skipped.append({"filename": name, "reason": "no download URL on Slack file object"})
            continue

        try:
            raw = await loop.run_in_executor(None, transcription._download_slack_file, url, SLACK_BOT_TOKEN)
        except Exception:
            log.exception("Failed to download Slack image %s", name)
            skipped.append({"filename": name, "reason": "download failed"})
            continue

        if len(raw) > MAX_IMAGE_BYTES:
            log.warning("Skipping image %s: downloaded size %s exceeds cap %s", name, len(raw), MAX_IMAGE_BYTES)
            skipped.append({"filename": name, "reason": f"too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES})"})
            continue

        sniffed_mimetype = _sniff_image_mimetype(raw)
        if sniffed_mimetype is None:
            log.warning("Skipping file %s: claimed image/* mimetype but failed magic-byte sniff", name)
            skipped.append({"filename": name, "reason": "not a recognized image format"})
            continue

        file_id = f.get("id") or uuid.uuid4().hex
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "image"
        local_path = conv_dir / f"{file_id}-{safe_name}"
        try:
            conv_dir.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(raw)
        except OSError:
            log.exception("Failed to write cached image to %s", local_path)
            skipped.append({"filename": name, "reason": "failed to write to local cache"})
            continue

        captured.append({
            "local_path": str(local_path),
            "slack_file_id": file_id,
            "filename": name,
            "mimetype": sniffed_mimetype,
            "size": len(raw),
        })

    return captured, skipped


async def _capture_images_and_prepare_text(
    event: dict, *, text: str, channel: str, ts: str, thread_ts: str | None,
    conversation_id: str | None, adapter: "SlackAdapter",
) -> tuple[str, bool]:
    """Shared by handle_dm and handle_mention so image capture covers both.

    Downloads/caches any image files on ``event`` via ``maybe_capture_images``,
    pre-records the user's message (with an ``[Attached image saved to: ...]``
    hint appended so the Copilot session can act on "save this photo"-style
    requests without a manual detour) so the new message_id exists for
    ``record_attachment`` to point at, and posts a status note for any file
    that was skipped (oversized/undownloadable/failed sniff) so a capture
    failure is never silent.

    Returns ``(text_to_forward, record_user_message)`` - when images were
    captured, the message has already been recorded here, so the caller
    must pass ``record_user_message=False`` to ``_handle`` to avoid a
    duplicate row.
    """
    captured, skipped = await maybe_capture_images(event, conversation_id=conversation_id)

    if skipped:
        reasons = "\n".join(f"- `{s['filename']}`: {s['reason']}" for s in skipped)
        await adapters.deliver(adapter,
            {"channel": channel, "thread_ts": thread_ts},
            f":warning: Couldn't capture {len(skipped)} attachment(s):\n{reasons}",
            conversation_id=conversation_id,
            role="system",
        )

    if not captured:
        return text, True

    hint = "\n".join(f"[Attached image saved to: {c['local_path']}]" for c in captured)
    forwarded_text = f"{text}\n{hint}".strip() if text else hint

    message_id = _record_visible_message(conversation_id, "user", forwarded_text, channel=channel, ts=ts)
    if message_id:
        for c in captured:
            message_store.record_attachment(
                message_id, conversation_id, kind="image",
                local_path=c["local_path"], slack_file_id=c["slack_file_id"],
                filename=c["filename"], mimetype=c["mimetype"], size=c["size"],
            )

    return forwarded_text, False


async def transcribe_web_upload(source, filename: str | None = None, *, conversation_id: str,
                                 user_id: str | None = None) -> None:
    """Web UI entry point: transcribe one browser recording via the shared
    ``_transcribe_one()`` core (adapter=``WebAdapter``).

    ``source`` is the Path of a recording already saved by ``audio_store``
    (the upload endpoint saves first). Raw ``bytes`` are accepted too and
    saved to the store here before anything else happens. Crash-resume is
    handled by the audio-store sidecar (see ``resume_audio_jobs``).
    """
    if isinstance(source, (bytes, bytearray)):
        source = await asyncio.get_running_loop().run_in_executor(
            None, lambda: audio_store.save_bytes(
                conversation_id, bytes(source), filename, platform="web", user_id=user_id))
    path = Path(source)
    await _run_audio_job(path, conversation_id=conversation_id, user_id=user_id)


async def _run_audio_job(path: Path, *, conversation_id: str, user_id: str | None,
                         claimed: bool = False) -> None:
    key = str(path)
    if not claimed:
        if key in _inflight_audio:
            return
        _inflight_audio.add(key)
    try:
        await _transcribe_one(
            name=audio_store.read(path).get("name") or path.name,
            transcribe=None,
            adapter=WebAdapter(),
            thread_key={"conversation_id": conversation_id},
            conversation_id=conversation_id,
            user_id=user_id,
            buf_key=conversation_id,
            pending_registration=None,
            audio_path=path,
        )
    finally:
        _inflight_audio.discard(key)


def start_audio_job(path: Path, *, conversation_id: str, user_id: str | None):
    """Fire-and-forget a saved Web UI recording (claims it so the resume sweep
    can't start it a second time). Returns the task, or None if already running."""
    key = str(path)
    if key in _inflight_audio:
        return None
    _inflight_audio.add(key)

    async def _go():
        try:
            await _run_audio_job(path, conversation_id=conversation_id, user_id=user_id, claimed=True)
        except Exception:
            log.exception("[%s] Background voice transcription failed", conversation_id)

    return asyncio.create_task(_go())


async def resume_audio_jobs(slack_adapter_factory=None) -> int:
    """Restart/maintenance sweep over the audio store: promote abandoned chunked
    uploads, then (re)start every unfinished job from its file on disk. A stored
    transcript is reused (no Whisper re-run), finished segments are skipped, and a
    job that already started ``audio_store.MAX_ATTEMPTS`` times is marked failed.
    One task per job; returns how many were started."""
    audio_store.promote_stale()
    started = 0
    for path, meta in audio_store.list_unfinished():
        key = str(path)
        platform = meta.get("platform")
        if key in _inflight_audio or platform not in ("web", "slack"):
            continue
        conversation_id = meta.get("conversation_id")
        if platform == "web" and message_store.get_conversation(conversation_id or "") is None:
            audio_store.update(path, state="failed", error="conversation no longer exists")
            continue
        if platform == "slack":
            if slack_adapter_factory is None:
                continue
            adapter = slack_adapter_factory()
            conversation_id = meta.get("conversation_ref") or None
        else:
            adapter = WebAdapter()
        thread_key = meta.get("thread_key") or {"conversation_id": conversation_id}
        if meta.get("transcript") is None and meta.get("attempts", 0) >= audio_store.MAX_ATTEMPTS:
            audio_store.update(path, state="failed", error="gave up after repeated restarts")
            if meta.get("message_ref"):
                try:
                    await adapters.deliver_update(
                        adapter, thread_key, meta["message_ref"],
                        ":x: Could not transcribe audio (gave up after repeated restarts); "
                        f"the recording is kept at `{path}`.",
                        conversation_id=conversation_id, role="assistant",
                        message_id=meta.get("message_id"), buttons=[])
                except Exception:
                    log.warning("Could not update placeholder for failed audio job %s", path)
            continue
        _inflight_audio.add(key)
        started += 1
        asyncio.create_task(_resume_audio_job(path, meta, adapter, thread_key, conversation_id))
    if started:
        log.info("Resuming %d unfinished audio transcription job(s) from %s", started, audio_store.ROOT)
    return started


async def _resume_audio_job(path: Path, meta: dict, adapter, thread_key: dict,
                            conversation_id: str | None) -> None:
    key = str(path)
    try:
        message_ref = meta.get("message_ref")
        message_id = meta.get("message_id")
        if not message_ref:
            # Crashed before a placeholder was posted: _transcribe_one posts one.
            await _transcribe_one(
                name=meta.get("name") or path.name, transcribe=None, adapter=adapter,
                thread_key=thread_key, conversation_id=conversation_id, user_id=meta.get("user_id"),
                buf_key=conversation_id or thread_key.get("channel", ""),
                pending_registration=None, audio_path=path)
            return
        try:
            await adapters.deliver_update(
                adapter, thread_key, message_ref,
                ":repeat: _Bot restarted mid-transcription — retrying..._",
                conversation_id=conversation_id, role="status", message_id=message_id, buttons=[])
        except Exception:
            log.warning("Could not update placeholder for resumed audio job %s", path)
        await _transcribe_and_finalize(
            name=meta.get("name") or path.name, transcribe=None, adapter=adapter,
            thread_key=thread_key, conversation_id=conversation_id, user_id=meta.get("user_id"),
            message_ref=message_ref, buf_key=conversation_id or thread_key.get("channel", ""),
            message_id=message_id, clear_pending=None, audio_path=path)
    except Exception:
        log.exception("[audio-resume] Failed to resume %s", path)
    finally:
        _inflight_audio.discard(key)


async def audio_maintenance_loop(slack_adapter_factory=None) -> None:
    """Startup + every minute: resume unfinished/abandoned audio jobs; once a day
    delete ``done`` recordings past the retention window."""
    last_cleanup = 0.0
    requeued = audio_store.retry_failed(startup=True)
    if requeued:
        log.info("Re-queued %d failed audio job(s) for one more attempt after restart", len(requeued))
    while True:
        try:
            if time.time() - last_cleanup > 86400:
                last_cleanup = time.time()
                removed = audio_store.cleanup()
                if removed:
                    log.info("Audio retention: removed %d finished recording(s)", removed)
            await resume_audio_jobs(slack_adapter_factory)
        except Exception:
            log.exception("audio maintenance sweep failed")
        await asyncio.sleep(60)


async def _transcribe_one(*, name: str, transcribe, adapter, thread_key: dict,
                           conversation_id: str | None, user_id: str | None,
                           buf_key: str, pending_registration: dict | None,
                           audio_path: Path | None = None) -> None:
    """Platform-agnostic core shared by Slack (``maybe_transcribe_audio``)
    and Web UI (``transcribe_web_upload``): post a "Transcribing..."
    placeholder (via ``adapter``, so Slack gets a real block-kit button and
    the Web UI renders the neutral ``buttons`` as message actions), then runs
    ``transcribe`` (an async callable taking an ``on_queued`` callback, wrapping either
    ``transcribe_slack_audio`` or ``transcription.transcribe_audio_bytes``)
    and finalizes the result.

    ``pending_registration`` (Slack-only — see ``maybe_transcribe_audio``'s
    docstring) is a dict of ``{"channel", "thread_ts", "file_info"}`` used to
    persist a crash-resumable record; pass ``None`` to skip. When ``audio_path``
    (a recording already saved in ``audio_store``) is given, transcription runs
    from that file and the sidecar tracks the job (see ``audio_store``) so a
    restart resumes it; ``transcribe`` may then be ``None``.
    """
    if AUDIO_AUTO_SEND:
        override_action_id = "preview_transcript"
        override_button_text = ":eye: Preview before sending"
    else:
        override_action_id = "auto_send_transcript"
        override_button_text = ":zap: Auto-send when ready"
    posted, message_id = await adapters.deliver(adapter,
        thread_key,
        ":headphones: _Transcribing..._",
        conversation_id=conversation_id,
        role="status",
        buttons=[Button(label=override_button_text, action_id=override_action_id)],
    )
    # `message_ref` is the adapter-native handle used for update()/delete()
    # calls — for Slack this is the real message ``ts``; the Web UI has no
    # such native concept (WebAdapter.update() only keys off message_id), so
    # fall back to the just-recorded message_id, which is never None once
    # send() succeeds.
    message_ref = None
    try:
        message_ref = adapter.message_ref(thread_key, posted, message_id).id or None
    except Exception:
        pass
    if message_ref is None:
        message_ref = message_id

    if audio_path is not None:
        audio_store.update(
            audio_path, message_id=message_id, message_ref=str(message_ref) if message_ref else None,
            thread_key=thread_key, platform=getattr(adapter, "platform", None) or "web",
            user_id=user_id, conversation_id=conversation_id,
        )

    if pending_registration is not None and message_ref:
        _register_pending_transcription(
            pending_registration["channel"], message_ref,
            thread_ts=pending_registration["thread_ts"], conversation_id=conversation_id,
            user_id=user_id, file_info=pending_registration["file_info"], message_id=message_id,
        )

    await _transcribe_and_finalize(
        name=name, transcribe=transcribe, adapter=adapter, thread_key=thread_key,
        conversation_id=conversation_id, user_id=user_id, message_ref=message_ref,
        buf_key=buf_key, message_id=message_id,
        clear_pending=pending_registration["channel"] if pending_registration else None,
        audio_path=audio_path,
    )


async def _transcribe_and_finalize(*, name: str, transcribe, adapter, thread_key: dict,
                                    conversation_id: str | None, user_id: str | None,
                                    message_ref, buf_key: str, message_id: str | None,
                                    clear_pending: str | None, audio_path: Path | None = None) -> None:
    """Run Whisper (via the ``transcribe`` callable) on one
    already-downloaded/already-in-hand audio input and finalize its
    placeholder message (``message_ref``, already posted as
    ":headphones: _Transcribing..._" by ``_transcribe_one()``). Shared by
    the live path and the resume-after-restart path, which use identical
    buffering/auto-send logic regardless of adapter.

    ``clear_pending`` is the Slack ``channel`` to clear the pending-
    transcription persistence entry for once done, or ``None`` (Web UI,
    where nothing was registered in the first place).

    ``audio_path`` (audio-store file): transcribe from disk (``transcribe`` is
    ignored), track the sidecar state, reuse a stored transcript instead of
    re-running Whisper, and mark the job ``done``/``delivered`` only after the
    transcript was delivered. The audio file is never deleted here.
    """

    async def _mark_queued(_message_ref=message_ref) -> None:
        if not _message_ref:
            return
        queued_text = ":hourglass_flowing_sand: _Queued — waiting for another transcription to finish..._"
        try:
            await adapters.deliver_update(adapter,
                thread_key,
                _message_ref,
                queued_text,
                conversation_id=conversation_id,
                role="status",
                message_id=message_id,
                buttons=[],
            )
        except Exception:
            log.debug("Could not mark transcription as queued for %s", _message_ref)

    async def _progress(done: int, total: int) -> None:
        if not message_ref:
            return
        await adapters.deliver_update(adapter, thread_key, message_ref,
            f":headphones: _Transcribing... segment {done}/{total}_",
            conversation_id=conversation_id, role="status", message_id=message_id, buttons=[])

    try:
        try:
            if audio_path is not None:
                meta = audio_store.read(audio_path)
                if meta.get("transcript") is not None:
                    text = meta["transcript"]
                else:
                    audio_store.update(audio_path, state="transcribing",
                                       attempts=meta.get("attempts", 0) + 1)
                    text = await transcription.transcribe_audio_file(
                        audio_path, on_queued=_mark_queued, on_progress=_progress)
                    audio_store.update(audio_path, transcript=text, state="transcribed")
            else:
                text = await transcribe(_mark_queued)
            reply = text or "_(no speech detected)_"
        except Exception as exc:
            log.exception("Audio transcription failed for %s", name)
            reply = None
            error_reply = f":x: Could not transcribe audio: {exc}"
            if audio_path is not None:
                audio_store.update(audio_path, state="failed", error=str(exc))
    finally:
        if clear_pending and message_ref:
            _clear_pending_transcription(clear_pending, message_ref)

    if reply is None:
        if message_ref:
            await adapters.deliver_update(adapter,
                thread_key,
                message_ref,
                error_reply,
                conversation_id=conversation_id,
                role="assistant",
                message_id=message_id,
                buttons=[],
            )
        else:
            await adapters.deliver(adapter,
                thread_key,
                error_reply,
                conversation_id=conversation_id,
                role="assistant",
            )
        return

    await _deliver_transcript(
        adapter=adapter, thread_key=thread_key, conversation_id=conversation_id, user_id=user_id,
        message_ref=message_ref, buf_key=buf_key, message_id=message_id, reply=reply,
        on_delivered=(lambda: audio_store.mark_done(audio_path)) if audio_path is not None else None)
    if audio_path is not None:
        audio_store.mark_done(audio_path)


async def _deliver_transcript(*, adapter, thread_key: dict, conversation_id: str | None,
                              user_id: str | None, message_ref, buf_key: str,
                              message_id: str | None, reply: str, on_delivered=None) -> None:
    """Show/forward the transcript. ``on_delivered`` fires once the transcript is
    in the conversation — for auto-send that is BEFORE the (long) Copilot turn
    starts, so a restart mid-turn doesn't make resume forward it a second time."""
    buf = _channel_audio_buffer.setdefault(buf_key, [])

    for entry in buf:
        plain_text = f":microphone: {entry['text']}"
        try:
            await adapters.deliver_update(adapter,
                thread_key,
                entry["ts"],
                plain_text,
                conversation_id=conversation_id,
                role="assistant",
                message_id=entry.get("message_id"),
                buttons=[],
            )
        except Exception:
            log.debug("Could not strip button from earlier transcript message %s", entry["ts"])

    buf.append({"ts": message_ref, "text": reply, "message_id": message_id})

    effective_mode = _transcript_mode_override.pop(message_ref, None) if message_ref else None
    if effective_mode is None:
        effective_mode = "auto" if AUDIO_AUTO_SEND else "manual"

    if effective_mode == "auto" and message_ref:
        # Web UI senders are already authenticated and may have no user id
        # (blank Slack allow-list), so don't require one there.
        if (user_id or getattr(adapter, "platform", None) == message_store.WEB_PLATFORM) and conversation_id:
            if on_delivered:
                on_delivered()
            await _send_transcript_buffer_to_copilot(
                adapter=adapter, thread_key=thread_key, message_ref=message_ref,
                conversation_id=conversation_id, user_id=user_id,
            )
            return
        log.warning("Auto-send mode active for %s but missing user_id/conversation_id; falling back to manual button", message_ref)

    merged_text = "\n\n".join(e["text"] for e in buf)
    final_text = f":microphone: {merged_text}"
    final_buttons = [Button(label=":arrow_right: Send to agent", action_id="send_transcript_to_copilot")]

    if message_ref:
        await adapters.deliver_update(adapter,
            thread_key,
            message_ref,
            final_text,
            conversation_id=conversation_id,
            role="assistant",
            message_id=message_id,
            buttons=final_buttons,
        )
    else:
        await adapters.deliver(adapter,
            thread_key,
            final_text,
            conversation_id=conversation_id,
            role="assistant",
            buttons=final_buttons,
        )


def is_authorized(user_id: str, *, platform: str = message_store.SLACK_PLATFORM) -> bool:
    # Fail CLOSED, not open: a blank/misconfigured allow-list for a platform
    # must never silently grant every user autopilot/--yolo access.
    if platform == message_store.SLACK_PLATFORM and ALLOWED_USER_ID:
        message_store.add_allowed_identity(platform, ALLOWED_USER_ID)
    allowed = message_store.is_allowed_identity(platform, user_id)
    if not allowed:
        log.error(
            "No allowed identity match for platform=%s user_id=%s — denying access",
            platform,
            user_id,
        )
    return allowed



async def _send_transcript_buffer_to_copilot(*, adapter, thread_key: dict, message_ref,
                                              conversation_id: str, user_id: str) -> None:
    """Pop the buffered transcript(s) for a conversation, strip the button
    from the message, and forward the merged text to Copilot. Shared by the
    manual "Send to agent" button (Slack-only today) and the "auto-send
    when ready" flow (both Slack and Web UI).
    """
    buf = _channel_audio_buffer.pop(conversation_id, [])
    text = "\n\n".join(e["text"] for e in buf)
    if not text:
        await adapters.deliver(adapter,
            thread_key,
            ":warning: Transcript no longer available.",
            conversation_id=conversation_id,
            role="system",
        )
        return

    # Adapters with an external identity (Slack ts) map the ref to the stored
    # row via external_ref; store-backed ones (Web) already pass the row id.
    ext = adapter.external_ref(adapter.message_ref(thread_key, message_ref))
    transcript_message_id = (
        message_store.find_message_id_by_external_ref(conversation_id, ext) if ext else message_ref
    )
    try:
        await adapters.deliver_update(adapter,
            thread_key,
            message_ref,
            f":microphone: {text}",
            conversation_id=conversation_id,
            role="assistant",
            message_id=transcript_message_id,
            buttons=[],
        )
    except Exception:
        log.exception("Could not remove button from transcript message")

    thread_ts = thread_key.get("thread_ts") or message_ref
    channel = thread_key.get("channel", "")
    await _handle(user_id=user_id, text=text, ts=message_ref, thread_ts=thread_ts,
                  conversation_id=conversation_id, record_user_message=False,
                  adapter=adapter, thread_key={**thread_key, "thread_ts": thread_ts},
                  authorized=True)


async def _arm_transcript_mode(mode: str, *, adapter, thread_key: dict, message_ref,
                                message_id: str | None, conversation_id: str | None,
                                current_text: str) -> None:
    """Platform-agnostic core for the "Auto-send"/"Preview before sending"
    toggle shown on a still-transcribing placeholder: arms an override for
    this message (``_transcribe_and_finalize`` checks it once the
    transcription finishes) and re-renders the placeholder so the user sees
    their choice took effect. ``mode`` is ``"auto"`` or ``"manual"``.

    Shared by the Slack ``auto_send_transcript``/``preview_transcript``
    action handlers and the Web UI's equivalent action endpoint — before
    this existed, the Web UI's only option (per the adapter-generalization
    critique) would have been a single global ``AUDIO_AUTO_SEND`` setting
    with no interactive per-message toggle, which is a real feature
    downgrade compared to Slack's buttons, not true parity.
    """
    _transcript_mode_override[message_ref] = mode
    if mode == "auto":
        note = ":zap: _Will send to the agent automatically once ready…_"
    else:
        note = ":eye: _Will show the text for review instead of auto-sending…_"
    try:
        await adapters.deliver_update(adapter,
            thread_key,
            message_ref,
            current_text,
            conversation_id=conversation_id,
            role="status",
            message_id=message_id,
            footnote=note,
            buttons=[],
        )
    except Exception:
        log.exception("Could not update transcript message after arming %s mode", mode)


async def web_transcript_action(action_id: str, *, conversation_id: str, message_id: str,
                                 user_id: str | None) -> bool:
    """Web UI entry point for the 3 transcript-placeholder buttons
    (``auto_send_transcript``/``preview_transcript``/``send_transcript_to_copilot``
    — see ``_arm_transcript_mode``/``_send_transcript_buffer_to_copilot``).
    ``message_id`` is the Web UI message id the button lives on, which for
    the Web platform IS the adapter-native ``message_ref`` (see
    ``_transcribe_one``'s fallback — the Web UI has no separate native
    handle). Returns True if ``action_id`` was recognized and handled,
    False otherwise (caller should respond 404/400).
    """
    adapter = WebAdapter()
    thread_key = {"conversation_id": conversation_id}

    if action_id == "send_transcript_to_copilot":
        await _send_transcript_buffer_to_copilot(
            adapter=adapter, thread_key=thread_key, message_ref=message_id,
            conversation_id=conversation_id, user_id=user_id or "",
        )
        return True

    if action_id in ("auto_send_transcript", "preview_transcript"):
        messages = message_store.get_messages(conversation_id)
        current = next((m for m in messages if m["id"] == message_id), None)
        current_text = current["content"] if current else ":headphones: _Transcribing..._"
        mode = "auto" if action_id == "auto_send_transcript" else "manual"
        await _arm_transcript_mode(
            mode, adapter=adapter, thread_key=thread_key, message_ref=message_id,
            message_id=message_id, conversation_id=conversation_id, current_text=current_text,
        )
        return True

    return False


_seen_incoming_refs: "OrderedDict[tuple[str, str], None]" = OrderedDict()
_SEEN_INCOMING_MAX = 2000


def _is_duplicate_incoming(msg: IncomingMessage) -> bool:
    """Platforms redeliver updates after reconnects; dedup on ``msg.ref``
    (bounded memory). A message without a ref can't be deduped."""
    if not msg.ref:
        return False
    return not first_sighting(_seen_incoming_refs, (msg.platform, msg.ref), _SEEN_INCOMING_MAX)


async def handle_incoming(msg: IncomingMessage, *, registry=None, run_turn=None) -> None:
    """Shared inbound path for any adapter's listener (a new platform plugs in
    here instead of copying the Slack glue): authorize by
    ``allowed_identities`` for ``msg.platform`` -> drop redelivered refs ->
    resolve the conversation from the adapter binding -> run the turn ->
    reply through the SAME adapter.

    Unauthorized senders get no turn and no reply (a silent drop avoids
    confirming the bot exists to strangers on public platforms).
    ``run_turn(conversation_id, text) -> reply text`` is the turn engine; by
    default the full ``_handle`` engine runs and delivers its own replies.

    Slack's Bolt listeners (``handle_dm``/``handle_mention``) deliberately keep
    their own edge - they need Slack-only pre-steps (subtype filtering, audio
    and image capture, thread-root/conversation keying) before a turn - but
    they share this module's ``_handle`` engine and the ``first_sighting``
    dedup primitive, so a new platform only writes its listener + adapter."""
    registry = registry or adapters.REGISTRY
    adapter = registry.get(msg.platform)
    if adapter is None:
        log.warning("Incoming %s message but no adapter registered - dropping", msg.platform)
        return
    if not is_authorized(msg.user_id, platform=msg.platform):
        return
    if _is_duplicate_incoming(msg):
        log.info("Dropping duplicate incoming %s message %s", msg.platform, msg.ref)
        return
    conversation_id = message_store.get_or_create_bound_conversation(msg.platform, msg.conversation_key)
    thread_key = adapter.thread_key_from_binding(msg.conversation_key) or {}
    if run_turn is None:
        await _handle(msg.user_id, msg.text, ts=msg.ref or "", conversation_id=conversation_id,
                      adapter=adapter, thread_key=thread_key, authorized=True)
        return
    message_store.record_message(conversation_id, "user", msg.text, external_ref=msg.ref)
    reply = await run_turn(conversation_id, msg.text)
    if reply:
        await adapters.deliver(adapter, thread_key, reply, conversation_id=conversation_id, role="assistant")


async def _handle(user_id: str, text: str, *, ts: str = "",
                  thread_ts: str | None = None, conversation_id: str | None = None,
                  record_user_message: bool = True,
                  adapter=None, thread_key: dict | None = None,
                  authorized: bool = False) -> None:
    """Shared handler for DMs, mentions, watch fires and Web UI sends.

    ``adapter``/``thread_key`` say where replies go (callers resolve them via
    the adapter registry from the conversation binding); with neither, the
    conversation's registered delivery target is used. ``ts`` is the
    triggering message's platform id (used for reactions/status bookkeeping)
    and ``thread_ts`` the thread root, both opaque to the core. The Slack
    ``channel`` used for crash-recovery bookkeeping is read from
    ``thread_key`` - there are no ``say``/``client``/``channel`` shims.
    ``authorized=True`` means the caller already authorized the sender for
    its own platform (``handle_incoming``); otherwise the legacy allow-list
    check runs here."""
    if adapter is not None and thread_key is None and conversation_id is not None:
        resolved = adapters.REGISTRY.delivery_for(conversation_id)
        thread_key = resolved[1] if resolved and resolved[0].platform == adapter.platform else {}
    channel = (thread_key or {}).get("channel") or ""
    if conversation_id is None:
        conversation_id = message_store.get_or_create_conversation(channel or None)
    if adapter is None:
        resolved = adapters.REGISTRY.delivery_for(conversation_id)
        if resolved:
            adapter, thread_key = resolved
        else:
            adapter = adapters.REGISTRY.get(message_store.WEB_PLATFORM) or WebAdapter()
            thread_key = {"conversation_id": conversation_id}
    elif thread_key is None:
        thread_key = {}
    channel = thread_key.get("channel") or ""
    thread_ts = thread_ts or thread_key.get("thread_ts")
    caps = adapter.capabilities
    client = getattr(adapter, "client", None)

    if not authorized and not is_authorized(user_id):
        log.warning("Unauthorized message from user_id=%s ignored", user_id)
        return

    if not text:
        return

    if record_user_message:
        _record_visible_message(conversation_id, "user", text, channel=channel, ts=ts)

    cmd = text.lower().strip()
    if (cmd.startswith("/md ") or cmd.startswith("/render ") or
            cmd.startswith("md ") or cmd.startswith("render ")):
        raw_arg = text.strip().split(None, 1)[1].strip() if len(text.strip().split(None, 1)) > 1 else ""
        md_path = Path(raw_arg).expanduser() if raw_arg else None
        if md_path is not None and not md_path.is_absolute():
            md_path = Path(COPILOT_WORKDIR) / md_path
        if md_path is not None and md_path.is_file():
            try:
                md_text = md_path.read_text(encoding="utf-8")
                pages = render_markdown_doc_png(md_text)
            except Exception as exc:
                log.exception("Failed to render Markdown file %s", md_path)
                await adapters.deliver(adapter,
                    thread_key,
                    f":x: Could not render `{md_path.name}`: {exc}",
                    conversation_id=conversation_id,
                    role="assistant",
                )
                return
            total = len(pages)
            try:
                if total > 1:
                    await adapter.upload_file(
                        thread_key,
                        file_uploads=[
                            {
                                "file": png_bytes,
                                "filename": f"{md_path.stem}-p{i}of{total}.png",
                                "title": f"{md_path.name} (page {i}/{total})",
                            }
                            for i, png_bytes in enumerate(pages, start=1)
                        ],
                    )
                else:
                    await adapter.upload_file(
                        thread_key,
                        pages[0],
                        f"{md_path.stem}.png",
                        title=md_path.name,
                    )
                _record_visible_message(
                    conversation_id,
                    "assistant",
                    f":frame_with_picture: Rendered `{md_path.name}` and uploaded {total} image(s).",
                )
            except Exception as exc:
                log.warning("[%s] Failed to upload rendered Markdown image: %s", channel, exc)
                await adapters.deliver(adapter,
                    thread_key,
                    f":x: Rendered but failed to upload: {exc}",
                    conversation_id=conversation_id,
                    role="assistant",
                )
            return

    convo_is_new = _is_new_conversation(conversation_id)
    convo = get_convo(conversation_id)
    thread_context: str | None = None
    if convo_is_new:
        try:
            await convo.ensure_ready()
            await adapters.deliver(adapter,
                thread_key,
                f":thread: New session: `{convo.session_id}`",
                conversation_id=conversation_id,
                role="system",
            )
        except Exception as exc:
            log.warning("[%s] Could not announce new session id: %s", conversation_id, exc)
        try:
            thread_context = await _session_context(
                client, channel, thread_ts, ts, conversation_id,
                **({"platform": adapter.platform, "thread_key": thread_key}
                   if adapter.platform == message_store.TELEGRAM_PLATFORM else {}))
        except Exception as exc:
            log.warning("[%s] Could not build session context: %s", conversation_id, exc)

    if caps.supports_reactions:
        try:
            await adapter.add_reaction(thread_key, "hourglass_flowing_sand", ts)
        except Exception:
            pass
        if thread_ts and thread_ts != ts:
            try:
                await adapter.add_reaction(thread_key, "hourglass_flowing_sand", thread_ts)
            except Exception:
                pass

    model_suffix = f" ({convo.model_name})" if convo.model_name else ""

    stop_button = Button(
        label=":black_square_for_stop: Stop", action_id="stop_working_turn",
        value=conversation_id, style="danger", native_only=True,
    )

    status_ts: str | None = None
    status_platform = adapter.platform
    status_conv_key = channel
    status_message_id: str | None = None
    initial_status_text = f":thinking_face: _Working...{model_suffix}_"
    try:
        result, status_message_id = await adapters.deliver(adapter,
            thread_key,
            initial_status_text,
            conversation_id=conversation_id,
            role="status",
            buttons=[stop_button],
        )
        status_ref = None
        if result and not caps.store_backed:
            status_ref = adapter.message_ref(thread_key, result, status_message_id)
        status_ts = status_ref.id if status_ref else None
        if status_ts:
            status_platform = status_ref.platform
            status_conv_key = status_ref.conversation_key
            _register_active_status(status_conv_key, status_ts, thread_ts, msg_ts=ts,
                                    platform=status_platform, thread_key=thread_key,
                                    conversation_id=conversation_id, message_id=status_message_id)
    except Exception as exc:
        log.warning("Could not post status message: %s", exc)

    status_editor = adapters.ThrottledEditor(adapter, thread_key)

    async def update_status(status_text: str) -> None:
        max_chars = adapter.capabilities.max_chars
        if max_chars and len(status_text) > max_chars:
            status_text = status_text[: max_chars - 1] + "…"
        if status_ts:
            try:
                await status_editor.edit(
                    status_ts,
                    status_text,
                    conversation_id=conversation_id,
                    role="status",
                    message_id=status_message_id,
                    buttons=[stop_button],
                )
            except Exception as exc:
                log.debug("Status message update failed: %s", exc)
        elif status_message_id:
            _record_visible_message(conversation_id, "status", status_text, message_id=status_message_id)

    send_text = f"{thread_context}\n\n{text}" if thread_context else text
    try:
        response = await convo.send(send_text, on_status=update_status)
    except TimeoutError:
        log.warning("[%s] DOP Agent request timed out", channel)
        response = (
            ":warning: DOP Agent request timed out (a tool call likely got stuck). "
            "Your thread history is preserved — send another message to resume."
        )
    except Exception as exc:
        log.exception("[%s] Error from Copilot SDK", channel)
        response = f":x: Error: {exc}"

    # Terminal edits below must never be overtaken by a throttled status edit.
    await status_editor.close()

    # Don't clear the active-status marker right after convo.send(): a kill
    # before the response is persisted below would lose the response and leave
    # nothing for _resume_interrupted_turns() to recover, stranding the
    # "Working..." placeholder. Clear it only after persistence.
    if caps.supports_reactions:
        try:
            await adapter.remove_reaction(thread_key, "hourglass_flowing_sand", ts)
        except Exception:
            pass
        if thread_ts and thread_ts != ts:
            try:
                await adapter.remove_reaction(thread_key, "hourglass_flowing_sand", thread_ts)
            except Exception:
                pass

    if response is INTERRUPTED:
        if status_ts:
            interrupted_text = ":black_square_for_stop: _Interrupted — continuing with your new message..._"
            try:
                await adapters.deliver_update(adapter,
                    thread_key,
                    status_ts,
                    interrupted_text,
                    conversation_id=conversation_id,
                    role="system",
                    message_id=status_message_id,
                    buttons=[],
                )
            except Exception as exc:
                log.debug("Could not update interrupted status message: %s", exc)
        elif status_message_id:
            _record_visible_message(
                conversation_id,
                "system",
                ":black_square_for_stop: _Interrupted — continuing with your new message..._",
                message_id=status_message_id,
            )
        if status_ts:
            _clear_active_status(status_conv_key, status_ts, platform=status_platform)
        return

    final_response_text = response or "_(no output)_"
    if status_message_id:
        _record_visible_message(
            conversation_id,
            "assistant",
            final_response_text,
            message_id=status_message_id,
            channel=channel,
            ts=status_ts,
        )
    else:
        _record_visible_message(conversation_id, "assistant", final_response_text)

    if status_ts:
        _clear_active_status(status_conv_key, status_ts, platform=status_platform)

    if caps.store_backed:
        return

    if response and TABLE_RENDER_MODE == "image":
        segments = extract_and_render_tables(response)
    else:
        segments = [(final_response_text, None)]

    status_used = False
    rich_limit = caps.max_rich_chars or caps.max_chars
    for seg_text, png_bytes in segments:
        if png_bytes is not None:
            if status_ts and not status_used:
                status_used = True
                if caps.supports_delete:
                    try:
                        await adapter.delete_message(thread_key, status_ts)
                    except Exception as exc:
                        log.debug("Could not delete status message before table image: %s", exc)
            if caps.supports_uploads:
                try:
                    await adapter.upload_file(
                        thread_key, png_bytes, "table.png", wait_posted=True,
                    )
                except Exception as exc:
                    log.warning("[%s] Failed to upload table image: %s", channel, exc)
            else:
                chunks = adapter.format_markdown(seg_text) if seg_text else []
                for chunk in chunks:
                    await adapters.deliver(adapter,
                        thread_key, chunk, conversation_id=conversation_id, role="assistant",
                        record=False,
                    )
            continue

        chunks = adapter.format_markdown(seg_text if seg_text else "_(no output)_")
        for chunk in chunks:
            if status_ts and not status_used:
                if rich_limit is None or len(chunk) <= rich_limit:
                    status_used = True
                    try:
                        await adapters.deliver_update(adapter,
                            thread_key,
                            status_ts,
                            chunk,
                            conversation_id=conversation_id,
                            role="assistant",
                            message_id=status_message_id,
                            store_text=final_response_text,
                            buttons=[],
                        )
                        continue
                    except Exception as exc:
                        log.warning("Failed to replace status message with response: %s", exc)
                else:
                    status_used = True
                    try:
                        await adapters.deliver_update(adapter,
                            thread_key,
                            status_ts,
                            ":white_check_mark: _Done — see below_",
                            conversation_id=conversation_id,
                            role="assistant",
                            message_id=status_message_id,
                            store_text=final_response_text,
                            buttons=[],
                        )
                    except Exception as exc:
                        log.debug("Could not clear status message before long chunk: %s", exc)
            # This chunk is a continuation of the already-recorded
            # final_response_text (recorded above, once, under
            # status_message_id) — post it to Slack without writing a
            # second message_store row for the same logical reply.
            await adapters.deliver(adapter,
                thread_key, chunk, conversation_id=conversation_id, role="assistant",
                record=False,
            )


def register(app) -> None:
    """Register all Slack action/event handlers on ``app``. Call once,
    right after the ``AsyncApp`` is constructed in dop_agent.py."""

    @app.action("stop_working_turn")
    async def handle_stop_working_turn(ack, body, client):
        """User clicked ":black_square_for_stop: Stop" on a live "Working..." status message —
        abort that conversation's in-progress turn. The button's value carries
        the conversation_id (set in _handle() on the Stop ``Button``) so we can look
        up the right CopilotConversation without needing channel/thread parsing.
        """
        await ack()
        user_id = body["user"]["id"]
        if not is_authorized(user_id):
            log.warning("Unauthorized stop_working_turn click from user_id=%s", user_id)
            return
        channel = body["channel"]["id"]
        msg_ts = body["message"]["ts"]
        thread_ts = body.get("container", {}).get("thread_ts") or msg_ts
        conversation_id = body["actions"][0].get("value", "") or _conversation_id_for(channel, thread_ts)
        convo = _convos.get(conversation_id)
        if not convo or not await convo.interrupt():
            # Nothing to stop (turn already finished) — just drop the button so
            # a stale/late click doesn't look like it did nothing.
            try:
                already_finished_text = "_(already finished)_"
                await adapters.deliver_update(adapters.REGISTRY.slack_for(client),
                    {"channel": channel},
                    msg_ts,
                    already_finished_text,
                    conversation_id=conversation_id,
                    role="system",
                    message_id=_message_id_for_slack_message(conversation_id, channel, msg_ts),
                    buttons=[],
                )
            except Exception:
                log.debug("Could not update stale Stop-button message")


    @app.action("send_transcript_to_copilot")
    async def handle_send_transcript_to_copilot(ack, body, client):
        await ack()
        channel = body["channel"]["id"]
        user_id = body["user"]["id"]
        if not is_authorized(user_id):
            log.warning("Unauthorized send_transcript_to_copilot click from user_id=%s", user_id)
            return
        msg_ts = body["message"]["ts"]
        # The transcript/placeholder message is always posted as a thread reply
        # (see maybe_transcribe_audio, which always passes a truthy thread_ts),
        # so container.thread_ts should be the thread root. Fall back to msg_ts
        # just in case (e.g. very first message in a channel with no thread yet).
        thread_ts = body.get("container", {}).get("thread_ts") or msg_ts
        conversation_id = _conversation_id_for(channel, thread_ts)

        await _send_transcript_buffer_to_copilot(
            adapter=adapters.REGISTRY.slack_for(client), thread_key={"channel": channel, "thread_ts": thread_ts},
            message_ref=msg_ts, conversation_id=conversation_id, user_id=user_id,
        )


    @app.action("auto_send_transcript")
    async def handle_auto_send_transcript(ack, body, client):
        """User clicked ":zap: Auto-send when ready" on a still-transcribing
        placeholder message (shown when AUDIO_AUTO_SEND=false). Arms an "auto"
        override for this message ts via the shared ``_arm_transcript_mode``
        core — ``_transcribe_and_finalize`` checks it once the transcription
        finishes and forwards the result automatically instead of waiting
        for a manual button click.
        """
        await ack()
        channel = body["channel"]["id"]
        user_id = body["user"]["id"]
        if not is_authorized(user_id):
            log.warning("Unauthorized auto_send_transcript click from user_id=%s", user_id)
            return
        msg_ts = body["message"]["ts"]
        thread_ts = body.get("container", {}).get("thread_ts") or msg_ts
        conversation_id = _conversation_id_for(channel, thread_ts)

        text = SlackAdapter.action_message_text(body, ":headphones: _Transcribing..._")

        await _arm_transcript_mode(
            "auto", adapter=adapters.REGISTRY.slack_for(client), thread_key={"channel": channel},
            message_ref=msg_ts, message_id=_message_id_for_slack_message(conversation_id, channel, msg_ts),
            conversation_id=conversation_id, current_text=text,
        )


    @app.action("preview_transcript")
    async def handle_preview_transcript(ack, body, client):
        """User clicked ":eye: Preview before sending" on a still-transcribing
        placeholder message (shown when AUDIO_AUTO_SEND=true, the default).
        Arms a "manual" override for this message ts via the shared
        ``_arm_transcript_mode`` core so, once transcription finishes, the
        text is shown with a ":arrow_right: Send to agent" button instead
        of being auto-forwarded — letting the user read it before deciding.
        """
        await ack()
        channel = body["channel"]["id"]
        user_id = body["user"]["id"]
        if not is_authorized(user_id):
            log.warning("Unauthorized preview_transcript click from user_id=%s", user_id)
            return
        msg_ts = body["message"]["ts"]
        thread_ts = body.get("container", {}).get("thread_ts") or msg_ts
        conversation_id = _conversation_id_for(channel, thread_ts)

        text = SlackAdapter.action_message_text(body, ":headphones: _Transcribing..._")

        await _arm_transcript_mode(
            "manual", adapter=adapters.REGISTRY.slack_for(client), thread_key={"channel": channel},
            message_ref=msg_ts, message_id=_message_id_for_slack_message(conversation_id, channel, msg_ts),
            conversation_id=conversation_id, current_text=text,
        )


    @app.event("message")
    async def handle_dm(event, say, client):
        if event.get("bot_id"):
            return
        if _is_duplicate_event(event):
            log.info("Ignoring duplicate/retried event ts=%s", event.get("ts"))
            return
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return

        user_id = event.get("user", "")
        channel = event.get("channel", "")
        if not is_authorized(user_id):
            log.warning("Unauthorized file_share/message from user_id=%s ignored", user_id)
            return

        # Per-thread session key: a brand-new top-level DM message (no
        # thread_ts) starts its own thread+session, keyed by its own ts; a
        # reply within an existing thread reuses that thread's session.
        thread_ts = _thread_root(event)
        conversation_id = _conversation_id_for(channel, thread_ts)

        if subtype == "file_share":
            if await maybe_transcribe_audio(event, say, client, thread_ts=thread_ts,
                                              conversation_id=conversation_id, user_id=user_id):
                return

        text = event.get("text", "").strip()
        ts = event.get("ts", "")
        record_user_message = True
        if subtype == "file_share":
            text, record_user_message = await _capture_images_and_prepare_text(
                event, text=text, channel=channel, ts=ts, thread_ts=thread_ts,
                conversation_id=conversation_id, adapter=adapters.REGISTRY.slack_for(client),
            )

        await _handle(
            user_id=user_id,
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            conversation_id=conversation_id,
            record_user_message=record_user_message,
            adapter=adapters.REGISTRY.slack_for(client),
            thread_key={"channel": channel, "thread_ts": thread_ts},
        )


    @app.event("app_mention")
    async def handle_mention(event, say, client):
        if _is_duplicate_event(event):
            log.info("Ignoring duplicate/retried event ts=%s", event.get("ts"))
            return
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
        channel = event.get("channel", "")
        # Same per-thread keying as DMs. Thread replies only reach here if
        # the bot is re-@mentioned (Slack sends app_mention only for mentions).
        thread_ts = _thread_root(event)
        conversation_id = _conversation_id_for(channel, thread_ts)

        if is_authorized(event.get("user", "")) and await maybe_transcribe_audio(
                event, say, client, thread_ts=thread_ts, conversation_id=conversation_id,
                user_id=event.get("user", "")):
            return

        ts = event.get("ts", "")
        record_user_message = True
        # Must gate on is_authorized the same way maybe_transcribe_audio()
        # above does - without this check, an unauthorized user could get
        # the bot to download/cache an arbitrary attached file and write a
        # DB row before _handle()'s own auth check (later) ever runs.
        if event.get("files") and is_authorized(event.get("user", "")):
            text, record_user_message = await _capture_images_and_prepare_text(
                event, text=text, channel=channel, ts=ts, thread_ts=thread_ts,
                conversation_id=conversation_id, adapter=adapters.REGISTRY.slack_for(client),
            )

        await _handle(
            user_id=event.get("user", ""),
            text=text,
            ts=ts,
            thread_ts=thread_ts,
            conversation_id=conversation_id,
            record_user_message=record_user_message,
            adapter=adapters.REGISTRY.slack_for(client),
            thread_key={"channel": channel, "thread_ts": thread_ts},
        )
