"""handle_incoming through the REAL ``_handle`` engine on a non-Slack platform."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "adapter_contract"))

import handlers  # noqa: E402
import message_store  # noqa: E402
from adapters import AdapterRegistry, IncomingMessage  # noqa: E402
from contract_harness import make_fake  # noqa: E402
from test_handlers import FakeConversation  # noqa: E402


async def test_non_slack_user_authorized_by_handle_incoming_is_not_rejected_by_the_engine(monkeypatch):
    """A user authorized by handle_incoming for its own platform is not re-checked against the
    Slack allow-list by the default engine (_handle), so no ':no_entry: Not authorized' reply."""
    fake = make_fake().adapter
    message_store.add_allowed_identity(fake.platform, "tg-user-1")
    convo = FakeConversation(response="engine answer")
    monkeypatch.setattr(handlers, "get_convo", lambda cid: convo)
    monkeypatch.setattr(handlers, "_is_new_conversation", lambda cid: False)

    await handlers.handle_incoming(
        IncomingMessage(platform=fake.platform, user_id="tg-user-1", conversation_key="42", text="ping",
                        ref="fake:42:1"),
        registry=AdapterRegistry([fake]),
    )

    texts = [s["text"] for s in fake.sent] + [e["text"] for e in fake.edits]
    assert convo.send_calls and "engine answer" in " ".join(texts)
    assert not any("Not authorized" in t for t in texts)
