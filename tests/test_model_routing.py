"""
P3-S5 model routing for child/research sessions.

Feature: session_create(model=None, task_kind=None) can run a child on a
cheaper model; without it the session runs on the CLI default. Guarantees covered here:
  * task_kind -> model map (research etc. cheap; code/prod default);
  * explicit model beats task_kind; unavailable models fall back visibly;
  * prod/CHG/terraform-apply/deploy wording is NEVER routed to a cheaper
    model (fail closed), also when the cheap model is requested explicitly;
  * MODEL_ROUTING=off is the rollback (no model ever passed); dry only logs;
  * release-loop delegation needs a handoff brief (self-healing error);
  * the chosen model + reason is persisted and re-applied on create AND resume;
  * AIU estimates carry their rate source (exact/fitted/sdk).
"""

from __future__ import annotations

import asyncio

import pytest

import message_store
import model_routing as mr
from model_routing import resolve_model

DEF = "claude-sonnet-5.5"
AVAIL = frozenset({DEF, "claude-haiku-5.5", "claude-sonnet-5", "claude-opus-5.5"})


def r(**kw):
    kw.setdefault("available", AVAIL)
    kw.setdefault("mode", "on")
    kw.setdefault("default", DEF)
    return resolve_model(**kw)


@pytest.mark.parametrize("kind", ["research", "summary", "log-triage", "status-report", "poll-review"])
def test_read_only_task_kinds_route_to_cheap_model(kind):
    out = r(task_kind=kind, prompt="summarise the open MR comments")
    assert out.pass_model == "claude-haiku-5.5" and out.model == "claude-haiku-5.5"
    assert kind in out.reason


@pytest.mark.parametrize("kind", ["code", "design", "review", "prod-ops", "terraform", "change", None])
def test_code_and_ops_task_kinds_stay_on_default_model(kind):
    out = r(task_kind=kind)
    assert out.pass_model is None and out.model == DEF


def test_explicit_model_wins_over_task_kind():
    out = r(model="claude-sonnet-5", task_kind="research")
    assert out.pass_model == "claude-sonnet-5" and "explicit" in out.reason


def test_unavailable_model_falls_back_to_default_with_visible_note():
    out = r(model="claude-haiku-4.5")
    assert out.pass_model is None and out.model == DEF
    assert "not available" in out.note and "claude-haiku-4.5" in out.note


def test_unknown_task_kind_error_lists_valid_kinds():
    with pytest.raises(ValueError, match="valid:.*research"):
        r(task_kind="vibes")


@pytest.mark.parametrize("prompt", [
    "check CHG-12345 approvals", "run terraform apply in prd", "deploy the module to prod",
    "terragrunt apply on mig-prd", "prepare the change request",
])
def test_prod_affecting_wording_is_never_routed_to_cheap_model(prompt):
    out = r(task_kind="research", prompt=prompt)
    assert out.pass_model is None and out.model == DEF
    assert "guard" in out.note


def test_prod_guard_also_blocks_explicit_cheap_model_and_parent_title():
    assert r(model="claude-haiku-5.5", prompt="apply the CHG").pass_model is None
    assert r(task_kind="summary", parent_title="prod release PRJ-1").pass_model is None


def test_prod_guard_does_not_block_a_pricier_explicit_model():
    out = r(model="claude-opus-5.5", prompt="design the prod rollout")
    # opus-5.5 has no known price -> treated as possibly cheaper (fail closed)
    assert out.pass_model is None


def test_mode_off_is_the_rollback_and_never_passes_a_model():
    out = r(model="claude-haiku-5.5", task_kind="research", mode="off")
    assert out.pass_model is None and "MODEL_ROUTING=off" in out.note


def test_mode_dry_reports_would_be_route_but_changes_nothing():
    out = r(task_kind="research", mode="dry")
    assert out.pass_model is None and out.model == DEF and "would route to claude-haiku-5.5" in out.note


def test_release_loop_requires_handoff_brief_and_stays_on_default():
    with pytest.raises(ValueError, match="handoff brief"):
        r(task_kind="release-loop", prompt="release it")
    brief = ("Release example-app 1.2.3 (PRJ-1). MR https://gitlab.example.com/x/y/-/merge_requests/5 . "
             "Steps: pipeline green, review comments, merge, master pipeline, cinc-client on dev nodes. "
             "Definition of done: master pipeline green and cinc-client OK on dev. Stop and ask on failures. " * 2)
    out = r(task_kind="release-loop", prompt=brief)
    assert out.pass_model is None and out.model == DEF


def test_estimate_aiu_uses_rates_and_reports_source():
    sonnet5 = mr.estimate_aiu("claude-sonnet-5", cache_read=1_000_000, output_tokens=1_000_000)
    assert sonnet5["aiu"] == 20 + 1000 and "exact" in sonnet5["source"]
    haiku = mr.estimate_aiu("claude-haiku-5.5", cache_read=40_000, cache_write=3_000, output_tokens=1_000)
    assert haiku["aiu"] < 0.2
    assert mr.estimate_aiu("nope", output_tokens=1)["aiu"] is None


def test_live_sdk_rates_override_static_table():
    mr._reset_caches_for_tests()

    class P: input_price, cache_read_price, cache_write_price, output_price = 1, 2, 3, 4
    class B: token_prices = P
    class M: id = "x-model"; billing = B

    mr._remember([M])
    assert mr.rates_for("x-model") == ((1, 2, 3, 4), "sdk list_models")
    mr._reset_caches_for_tests()


def test_available_models_falls_back_to_static_list_when_sdk_call_fails():
    mr._reset_caches_for_tests()

    class Broken:
        async def list_models(self):
            raise RuntimeError("boom")

    got = asyncio.run(mr.available_models(Broken()))
    assert got == mr.STATIC_AVAILABLE


def test_available_models_caches_live_list():
    mr._reset_caches_for_tests()
    calls = []

    class M:
        id = "claude-haiku-5.5"
        billing = None

    class Ok:
        async def list_models(self):
            calls.append(1)
            return [M]

    assert asyncio.run(mr.available_models(Ok())) == {"claude-haiku-5.5"}
    asyncio.run(mr.available_models(Ok()))
    assert len(calls) == 1
    mr._reset_caches_for_tests()


def test_model_and_reason_are_persisted_per_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "t.json")
    message_store.init_db()
    cid = message_store.get_or_create_conversation(None)
    assert message_store.get_conversation_model(cid) is None
    message_store.set_conversation_model(cid, "claude-haiku-5.5", "task_kind=research -> cheap model")
    assert message_store.get_conversation_model(cid) == "claude-haiku-5.5"
    assert message_store.get_conversation(cid)["model_reason"].startswith("task_kind=research")


# --- copilot_session._ensure_session passes the stored model on create/resume --

class _FakeSDKSession:
    session_id = "sid"

    async def __aenter__(self):
        return self


class _FakeClient:
    def __init__(self, reject_model=False):
        self.calls = []
        self.reject_model = reject_model

    async def create_session(self, **kw):
        self.calls.append(("create", kw))
        if self.reject_model and "model" in kw:
            raise RuntimeError('Model "x" is not available.')
        return _FakeSDKSession()

    async def resume_session(self, sid, **kw):
        self.calls.append(("resume", kw))
        return _FakeSDKSession()


def _convo_with_client(tmp_path, monkeypatch, model, saved_sid=None, client=None):
    import copilot_session as cs
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "t.json")
    message_store.init_db()
    cid = message_store.get_or_create_conversation(None)
    if model:
        message_store.set_conversation_model(cid, model, "test")
    client = client or _FakeClient()
    monkeypatch.setattr(cs, "CopilotClient", lambda **kw: type("C", (), {
        "__aenter__": lambda s: asyncio.sleep(0), "create_session": client.create_session,
        "resume_session": client.resume_session})())
    monkeypatch.setattr(cs, "_get_saved_session_id", lambda c: saved_sid)
    monkeypatch.setattr(cs, "_set_saved_session_id", lambda c, s: None)
    return cs.CopilotConversation(cid), client, cid


def test_stored_model_is_passed_on_create_session(tmp_path, monkeypatch):
    convo, client, _ = _convo_with_client(tmp_path, monkeypatch, "claude-haiku-5.5")
    asyncio.run(convo._ensure_session())
    assert client.calls[0][0] == "create" and client.calls[0][1]["model"] == "claude-haiku-5.5"


def test_stored_model_is_reapplied_on_resume_session(tmp_path, monkeypatch):
    """A resumed child (bot restart / session reload) must keep its routed
    model instead of silently reverting to the CLI default."""
    convo, client, _ = _convo_with_client(tmp_path, monkeypatch, "claude-haiku-5.5", saved_sid="old")
    asyncio.run(convo._ensure_session())
    assert client.calls[0][0] == "resume" and client.calls[0][1]["model"] == "claude-haiku-5.5"


def test_no_stored_model_passes_no_model_kwarg(tmp_path, monkeypatch):
    convo, client, _ = _convo_with_client(tmp_path, monkeypatch, None)
    asyncio.run(convo._ensure_session())
    assert "model" not in client.calls[0][1]


def test_rejected_model_falls_back_to_default_and_clears_stored_model(tmp_path, monkeypatch):
    convo, client, cid = _convo_with_client(tmp_path, monkeypatch, "gone-model", client=_FakeClient(reject_model=True))
    asyncio.run(convo._ensure_session())
    assert [c[0] for c in client.calls] == ["create", "create"] and "model" not in client.calls[1][1]
    assert message_store.get_conversation_model(cid) is None
    assert any("rejected" in m["content"] for m in message_store.get_messages(cid) if m["role"] == "system")


def test_merge_and_approve_steps_are_never_routed_to_cheap_model():
    """A poll-review prompt that asks to merge/approve an MR is a change, not
    read-only polling, so it keeps the default model; read-only wording about merged MRs stays cheap."""
    assert r(task_kind="poll-review", prompt="merge the MR and watch master pipeline").pass_model is None
    assert r(task_kind="poll-review", prompt="approve !123 when green").pass_model is None
    assert r(task_kind="poll-review", prompt="list my recently merged MRs and their pipelines").pass_model == "claude-haiku-5.5"


def test_session_list_and_conversation_rows_expose_model(tmp_path, monkeypatch):
    monkeypatch.setattr(message_store, "DB_PATH", tmp_path / "b.db")
    monkeypatch.setattr(message_store, "_TITLES_FILE", tmp_path / "t.json")
    message_store.init_db()
    cid = message_store.get_or_create_conversation(None)
    message_store.set_conversation_model(cid, "claude-haiku-5.5", "task_kind=research -> cheap model")
    row = next(c for c in message_store.list_conversations() if c["conversation_id"] == cid)
    assert row["model"] == "claude-haiku-5.5" and row["model_reason"].startswith("task_kind")
