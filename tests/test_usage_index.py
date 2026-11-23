"""
Usage widget data layer (usage_index.py): token/AIU consumption shown in the
Web UI header, thread header and sidebar.

Feature: Copilot CLI's /usage only reports one live session. The bot instead
indexes every ~/.copilot/session-state/*/events.jsonl so the Web UI can show
today/7d/30d AIU across ALL sessions. Guarantees covered here:
  * totalNanoAiu checkpoints are CUMULATIVE: per-day deltas are stored, so a
    session spanning two days is split correctly and nothing is double counted;
  * indexing is incremental by byte offset: a second scan of an unchanged file
    does nothing, appended lines are added, a half-written last line is NOT
    consumed until its newline arrives;
  * a counter that goes DOWN (resume/reset) adds nothing and does not go negative;
  * a truncated file is re-indexed from scratch;
  * shutdown events provide the token-type split and context breakdown;
  * USAGE_WIDGET=off disables the feature (rollback flag).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import usage_index


def _ckpt(ts: str, aiu: float) -> str:
    return json.dumps({"type": "session.usage_checkpoint", "timestamp": ts, "data": {"totalNanoAiu": aiu * 1e9}}) + "\n"


def _shutdown(tokens: dict, ctx: dict) -> str:
    data = {"tokenDetails": {k: {"tokenCount": v} for k, v in tokens.items()}, **ctx}
    return json.dumps({"type": "session.shutdown", "timestamp": "2026-10-19T10:00:00Z", "data": data}) + "\n"


def _setup(tmp_path: Path, sid: str, lines: list[str]) -> tuple[Path, Path]:
    state = tmp_path / "state"
    (state / sid).mkdir(parents=True)
    (state / sid / "events.jsonl").write_text("".join(lines))
    return state, tmp_path / "usage.db"


def test_daily_deltas_split_a_cumulative_counter_across_days(tmp_path):
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-18T12:00:00Z", 10), _ckpt("2026-10-18T13:00:00Z", 25), _ckpt("2026-10-19T12:00:00Z", 30)])
    usage_index.scan(db, state)
    s = usage_index.summary(db, today=datetime(2026, 10, 19, 15, 0))
    by_day = {d["day"]: d["aiu"] for d in s["daily"]}
    assert by_day["2026-10-18"] == 25.0 and by_day["2026-10-19"] == 5.0
    assert s["totals"]["d7"] == 30.0 and s["totals"]["today"] == 5.0
    assert usage_index.aiu_by_session(db) == {"s1": 30.0}


def test_second_scan_is_noop_and_appended_lines_are_picked_up(tmp_path):
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-19T10:00:00Z", 10)])
    assert usage_index.scan(db, state) == 1
    assert usage_index.scan(db, state) == 0
    with open(state / "s1" / "events.jsonl", "a") as fh:
        fh.write(_ckpt("2026-10-19T11:00:00Z", 14))
    assert usage_index.scan(db, state) == 1
    assert usage_index.aiu_by_session(db)["s1"] == 14.0


def test_half_written_last_line_is_not_consumed_until_complete(tmp_path):
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-19T10:00:00Z", 10)])
    f = state / "s1" / "events.jsonl"
    full = _ckpt("2026-10-19T11:00:00Z", 20)
    with open(f, "a") as fh:
        fh.write(full[:30])
    usage_index.scan(db, state)
    assert usage_index.aiu_by_session(db)["s1"] == 10.0
    with open(f, "a") as fh:
        fh.write(full[30:])
    usage_index.scan(db, state)
    assert usage_index.aiu_by_session(db)["s1"] == 20.0


def test_counter_reset_adds_nothing_and_never_goes_negative(tmp_path):
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-19T10:00:00Z", 50), _ckpt("2026-10-19T11:00:00Z", 5), _ckpt("2026-10-19T12:00:00Z", 8)])
    usage_index.scan(db, state)
    assert usage_index.aiu_by_session(db)["s1"] == 53.0


def test_truncated_file_is_reindexed_from_scratch(tmp_path):
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-19T10:00:00Z", 10), _ckpt("2026-10-19T11:00:00Z", 40)])
    usage_index.scan(db, state)
    (state / "s1" / "events.jsonl").write_text(_ckpt("2026-10-19T10:00:00Z", 7))
    usage_index.scan(db, state)
    assert usage_index.aiu_by_session(db)["s1"] == 7.0


def test_shutdown_event_feeds_token_split_and_context(tmp_path):
    ctx = {"currentTokens": 58684, "systemTokens": 8186, "conversationTokens": 24465, "toolDefinitionsTokens": 26030}
    state, db = _setup(tmp_path, "s1", [_ckpt("2026-10-19T10:00:00Z", 10), _shutdown({"input": 30, "cache_read": 879110, "cache_write": 81072, "output": 16719}, ctx)])
    usage_index.scan(db, state)
    s = usage_index.summary(db, today=datetime(2026, 10, 19, 12, 0))
    split = {x["type"]: x for x in s["token_split"]}
    assert split["cache_read"]["tokens"] == 879110
    assert abs(sum(x["pct"] for x in s["token_split"]) - 100) < 0.5
    detail = usage_index.session_detail("s1", db_path=db)
    assert detail["context"]["currentTokens"] == 58684 and detail["aiu"] == 10.0


def test_widget_flag_off_disables_feature(monkeypatch):
    monkeypatch.setenv("USAGE_WIDGET", "off")
    assert usage_index.enabled() is False
    monkeypatch.delenv("USAGE_WIDGET")
    assert usage_index.enabled() is True


def test_ranges_provide_hourly_today_series_and_per_range_top_sessions(tmp_path):
    """1d/7d/30d switch in the hover report: '1' is 24 hourly buckets of today, '7'/'30' daily;
    top sessions are computed per range (an older session must not appear under 1d)."""
    now = datetime.now().astimezone()
    old = (now - timedelta(days=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state, db = _setup(tmp_path, "old", [_ckpt(old, 40)])
    (state / "new").mkdir()
    (state / "new" / "events.jsonl").write_text(_ckpt(cur, 12))
    usage_index.scan(db, state)
    s = usage_index.summary(db, today=datetime.now())
    r = s["ranges"]
    assert len(r["1"]["series"]) == 24 and r["1"]["unit"] == "hour"
    assert len(r["7"]["series"]) == 7 and len(r["30"]["series"]) == 30
    assert [t["session_id"] for t in r["1"]["top"]] == ["new"]
    assert {t["session_id"] for t in r["7"]["top"]} == {"old", "new"}
    assert sum(x["aiu"] for x in r["1"]["series"]) == 12.0


def test_token_split_is_computed_per_range_from_each_sessions_mix(tmp_path):
    """The donut must change with 1d/7d/30d: an old output-only session counts toward 7d but not
    1d, so the 1d split is the cache_read-only session's mix."""
    now = datetime.now().astimezone()
    old = (now - timedelta(days=3)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state, db = _setup(tmp_path, "old", [_ckpt(old, 40), _shutdown({"output": 1000}, {})])
    (state / "new").mkdir()
    (state / "new" / "events.jsonl").write_text(_ckpt(cur, 10) + _shutdown({"cache_read": 1000}, {}))
    usage_index.scan(db, state)
    r = usage_index.summary(db, today=datetime.now())["ranges"]
    d1 = {x["type"]: x["pct"] for x in r["1"]["token_split"]}
    d7 = {x["type"]: x["pct"] for x in r["7"]["token_split"]}
    assert d1["cache_read"] == 100.0 and d1["output"] == 0.0
    assert d7["output"] == 80.0 and d7["cache_read"] == 20.0


# --- quota refresh ---
import asyncio


def _quota_reset(monkeypatch):
    monkeypatch.setattr(usage_index, "_quota_cache", {"at": 0.0, "value": None, "fetched_at": None, "error": None, "tried": 0.0})


def test_quota_refreshes_after_ttl_instead_of_staying_frozen(monkeypatch):
    """Quota is re-fetched once the TTL (default 1h) has elapsed, not before,
    and exposes the REAL reset date from GitHub (the CLI RPC resetDate is only the snapshot time)."""
    _quota_reset(monkeypatch)
    calls = []

    def fake():
        calls.append(1)
        return {"entitlement": 100, "used": len(calls), "unlimited": False, "remaining_pct": 50, "used_pct": 1,
                "overage": 0, "reset_at": "2026-11-01T00:00:00.000Z"}

    monkeypatch.setattr(usage_index, "_http_quota", fake)
    first = asyncio.run(usage_index.fetch_quota(None))
    assert first["used"] == 1 and first["reset_at"].startswith("2026-11-01") and first["fetched_at"]
    assert asyncio.run(usage_index.fetch_quota(None))["used"] == 1  # within TTL: cached
    usage_index._quota_cache["at"] -= usage_index.quota_ttl() + 1
    assert asyncio.run(usage_index.fetch_quota(None))["used"] == 2  # after TTL: refreshed


def test_quota_failure_keeps_last_good_value_and_reports_error(monkeypatch):
    """A failed refresh must not blank the widget: last good value stays, with `error` set for the UI."""
    _quota_reset(monkeypatch)
    monkeypatch.setattr(usage_index, "_http_quota", lambda: {"entitlement": 10, "used": 3, "unlimited": False,
                        "remaining_pct": 70, "used_pct": 30, "overage": 0, "reset_at": "2026-11-01"})
    asyncio.run(usage_index.fetch_quota(None))
    usage_index._quota_cache["at"] -= usage_index.quota_ttl() + 1

    def boom():
        raise OSError("net down")

    monkeypatch.setattr(usage_index, "_http_quota", boom)
    res = asyncio.run(usage_index.fetch_quota(None))
    assert res["used"] == 3 and "net down" in res["error"]


def test_quota_force_refresh_is_rate_limited(monkeypatch):
    """On-demand refresh (?refresh=1) re-fetches only when the cached value is older than the short force gap."""
    _quota_reset(monkeypatch)
    n = []
    monkeypatch.setattr(usage_index, "_http_quota", lambda: n.append(1) or {"entitlement": 1, "used": len(n), "unlimited": False,
                        "remaining_pct": 1, "used_pct": 1, "overage": 0, "reset_at": None})
    asyncio.run(usage_index.fetch_quota(None))
    asyncio.run(usage_index.fetch_quota(None, force=True))
    assert len(n) == 1
    usage_index._quota_cache["at"] -= usage_index.QUOTA_FORCE_MIN_SECONDS + 1
    assert asyncio.run(usage_index.fetch_quota(None, force=True))["used"] == 2


def test_quota_falls_back_to_cli_rpc_without_reset_date_when_github_unavailable(monkeypatch):
    """If copilot_internal/user fails, the CLI account.getQuota RPC still supplies usage numbers (reset_at is
    None there - the UI then says the reset date is unavailable) instead of blanking the widget."""
    _quota_reset(monkeypatch)

    def boom():
        raise OSError("blocked")

    async def rpc(client):
        return {"entitlement": 10, "used": 4, "unlimited": False, "remaining_pct": 60, "used_pct": 40,
                "overage": 0, "reset_at": None, "source": "cli-rpc"}

    monkeypatch.setattr(usage_index, "_http_quota", boom)
    monkeypatch.setattr(usage_index, "_rpc_quota", rpc)
    res = asyncio.run(usage_index.fetch_quota(object()))
    assert res["used"] == 4 and res["reset_at"] is None and res["source"] == "cli-rpc" and res["error"] is None


def test_quota_loop_sleeps_until_value_goes_stale_and_refused_force_is_flagged(monkeypatch):
    """(a) The background loop wakes when the cached value is due (not a full TTL after any
    page-triggered fetch); (b) a forced refresh refused by the 30 s gap is flagged
    `refresh_limited` so the UI can say so instead of silently ignoring the click."""
    _quota_reset(monkeypatch)
    monkeypatch.setattr(usage_index, "_http_quota", lambda: {"entitlement": 1, "used": 1, "unlimited": False,
                        "remaining_pct": 0, "used_pct": 100, "overage": 0, "reset_at": None})
    assert usage_index.quota_next_due() == usage_index.QUOTA_RETRY_SECONDS  # nothing cached yet
    asyncio.run(usage_index.fetch_quota(None))
    usage_index._quota_cache["at"] -= 1000
    assert 60 <= usage_index.quota_next_due() <= usage_index.quota_ttl() - 999
    usage_index._quota_cache["at"] = __import__("time").monotonic()
    assert asyncio.run(usage_index.fetch_quota(None, force=True))["refresh_limited"] is True
    assert asyncio.run(usage_index.fetch_quota(None))["refresh_limited"] is False
