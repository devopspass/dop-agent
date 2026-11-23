"""
Copilot token/AIU usage index for the Web UI usage widget.

Data sources (all read-only w.r.t. Copilot):
  - ``~/.copilot/session-state/<session_id>/events.jsonl`` - every Copilot
    session (bot AND plain CLI). Only two event types are used:
      * ``session.usage_checkpoint`` -> ``totalNanoAiu`` is CUMULATIVE per
        session (nano-AIU, /1e9 = AIU); we store per-(session, local hour)
        DELTAS so a session spanning several days is attributed correctly.
      * ``session.shutdown`` -> last-seen token-type split (``tokenDetails``)
        and context breakdown (``currentTokens``/``systemTokens``/
        ``toolDefinitionsTokens``/``conversationTokens``). Written only on
        shutdown, so it is "last known", not live.
  - Live SDK events fed through :func:`note_live_event` (copilot_session.py):
    ``session.usage_info`` (context tokens / limit) and ``assistant.usage``
    (``cacheExpiresAt``) - in memory only, per bot conversation.
  - ``account.getQuota`` (premium_interactions) via :func:`fetch_quota`.

Incremental indexing: ``usage_files`` stores the byte offset per events file;
a scan only reads appended bytes, and only complete (newline-terminated)
lines, so a half-written last line is picked up next time. A file that got
smaller than its stored offset is re-indexed from scratch. The first scan
reads the whole archive (~1.4 GB, tens of seconds) so :func:`ensure_background_scan`
runs it in a daemon thread and ``summary()`` reports ``indexing: true``.

Gotchas:
  - A checkpoint total LOWER than the previous one (counter reset) adds
    nothing - resume restores the aggregate, so only positive deltas count.
  - The quota ``entitlementRequests`` unit (AIU vs premium requests) is
    UNVERIFIED; the UI shows a tooltip saying so.
  - Own sqlite file (``usage_index_v2.db``, a rebuildable cache) - never
    touches bot_store.db.
"""

from __future__ import annotations

import json
import logging
import os

import config
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("usage_index")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "usage_index_v2.db"
SESSION_STATE_DIR = Path(os.path.expanduser("~/.copilot/session-state"))

# Fitted AIU per million tokens (Sonnet-5); used only for the cost-weighted split.
RATES_PER_M = {"input": 200.0, "cache_read": 20.0, "cache_write": 250.0, "output": 1000.0}
HEAVY_SESSION_AIU = 5000.0
QUOTA_TTL_SECONDS = 3600  # default; config key agent.quota_ttl_s overrides
QUOTA_RETRY_SECONDS = 120  # back-off after a failed refresh (last good value is kept)
QUOTA_FORCE_MIN_SECONDS = 30  # min gap between on-demand (?refresh=1) fetches
COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
SCAN_MIN_INTERVAL = 20.0

_lock = threading.RLock()
_last_scan_at = 0.0
_scan_thread: threading.Thread | None = None
_initial_scan_done = False
_live: dict[str, dict[str, Any]] = {}
_quota_cache: dict[str, Any] = {"at": 0.0, "value": None, "fetched_at": None, "error": None, "tried": 0.0}


def enabled() -> bool:
    return bool(config.value("agent.usage_widget"))


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = str(db_path or DB_PATH)
    if not os.path.exists(path):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        except OSError:
            pass
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.chmod(path + suffix, 0o600)
        except OSError:
            pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage_files (sid TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0,"
        " last_nano REAL NOT NULL DEFAULT 0, last_ts TEXT, tokens_json TEXT, context_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS usage_hourly (sid TEXT NOT NULL, hour TEXT NOT NULL, aiu REAL NOT NULL,"
        " PRIMARY KEY (sid, hour))"
    )
    return conn


def _local_hour(ts: str) -> str:
    """Local-time bucket 'YYYY-MM-DD HH' (hourly granularity feeds the 1d view)."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H")
    except Exception:
        return ts[:10] + " 00"


def _scan_file(conn: sqlite3.Connection, sid: str, path: Path) -> bool:
    """Index appended bytes of one events file. Returns True if anything changed."""
    size = path.stat().st_size
    row = conn.execute("SELECT offset, last_nano, last_ts, tokens_json, context_json FROM usage_files WHERE sid=?", (sid,)).fetchone()
    offset, last_nano, last_ts, tokens_json, context_json = row if row else (0, 0.0, None, None, None)
    if size < offset:
        conn.execute("DELETE FROM usage_hourly WHERE sid=?", (sid,))
        offset, last_nano, last_ts, tokens_json, context_json = 0, 0.0, None, None, None
    if size == offset and row:
        return False
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return False
    consumed = end + 1
    deltas: dict[str, float] = {}
    for raw in chunk[:consumed].splitlines():
        if b'"session.usage_checkpoint"' in raw:
            try:
                ev = json.loads(raw)
                nano = float(ev["data"]["totalNanoAiu"]) / 1e9
            except Exception:
                continue
            if nano > last_nano:
                day = _local_hour(ev.get("timestamp", ""))
                deltas[day] = deltas.get(day, 0.0) + (nano - last_nano)
            last_nano = nano
            last_ts = ev.get("timestamp") or last_ts
        elif b'"session.shutdown"' in raw:
            try:
                data = json.loads(raw)["data"]
            except Exception:
                continue
            tokens = {k: int((v or {}).get("tokenCount", 0)) for k, v in (data.get("tokenDetails") or {}).items()}
            if tokens:
                tokens_json = json.dumps(tokens)
            ctx = {k: data.get(k) for k in ("currentTokens", "systemTokens", "toolDefinitionsTokens", "conversationTokens") if data.get(k) is not None}
            if ctx:
                context_json = json.dumps(ctx)
    for day, aiu in deltas.items():
        conn.execute(
            "INSERT INTO usage_hourly(sid, hour, aiu) VALUES(?,?,?) ON CONFLICT(sid, hour) DO UPDATE SET aiu = aiu + excluded.aiu",
            (sid, day, aiu),
        )
    conn.execute(
        "INSERT OR REPLACE INTO usage_files(sid, offset, last_nano, last_ts, tokens_json, context_json) VALUES(?,?,?,?,?,?)",
        (sid, offset + consumed, last_nano, last_ts, tokens_json, context_json),
    )
    return True


def scan(db_path: Path | None = None, state_dir: Path | None = None) -> int:
    """Incrementally index every events.jsonl. Returns the number of changed files."""
    global _last_scan_at, _initial_scan_done
    state_dir = state_dir or SESSION_STATE_DIR
    changed = 0
    with _lock:
        conn = _connect(db_path)
        try:
            if state_dir.is_dir():
                for d in state_dir.iterdir():
                    f = d / "events.jsonl"
                    if not f.is_file():
                        continue
                    try:
                        if _scan_file(conn, d.name, f):
                            changed += 1
                    except Exception:
                        log.exception("usage scan failed for %s", f)
            conn.commit()
        finally:
            conn.close()
        _last_scan_at = time.monotonic()
        _initial_scan_done = True
    return changed


def ensure_background_scan(force: bool = False) -> None:
    """Start a daemon scan thread unless one runs or the last scan is fresh."""
    global _scan_thread
    with _lock:
        if _scan_thread is not None and _scan_thread.is_alive():
            return
        if not force and _initial_scan_done and time.monotonic() - _last_scan_at < SCAN_MIN_INTERVAL:
            return
        _scan_thread = threading.Thread(target=scan, name="usage-scan", daemon=True)
        _scan_thread.start()


def indexing() -> bool:
    return not _initial_scan_done


def aiu_by_session(db_path: Path | None = None) -> dict[str, float]:
    """All-time AIU per Copilot session id (sidebar / thread-header badge)."""
    conn = _connect(db_path)
    try:
        return {sid: aiu for sid, aiu in conn.execute("SELECT sid, SUM(aiu) FROM usage_hourly GROUP BY sid")}
    finally:
        conn.close()


_aiu_map_cache: dict[str, Any] = {"at": 0.0, "map": {}}


def aiu_by_session_cached(max_age: float = 15.0) -> dict[str, float]:
    """aiu_by_session() memoised for max_age seconds (the sessions SSE payload is rebuilt often)."""
    now = time.monotonic()
    if now - _aiu_map_cache["at"] > max_age:
        try:
            _aiu_map_cache.update(at=now, map=aiu_by_session())
        except Exception:
            log.exception("aiu_by_session failed")
            _aiu_map_cache.update(at=now)
    return _aiu_map_cache["map"]


def _split(tokens: dict[str, int]) -> list[dict[str, Any]]:
    weighted = {k: tokens.get(k, 0) * r / 1e6 for k, r in RATES_PER_M.items()}
    total = sum(weighted.values()) or 1.0
    return [{"type": k, "tokens": tokens.get(k, 0), "aiu": round(v, 1), "pct": round(100 * v / total, 1)} for k, v in weighted.items()]


def summary(db_path: Path | None = None, today: datetime | None = None, top_n: int = 8) -> dict[str, Any]:
    """Totals (today/7d/30d), 30-day daily series, burn rate, top sessions and token-type split."""
    now = today or datetime.now()
    day = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    conn = _connect(db_path)
    try:
        def total(since: str) -> float:
            return conn.execute("SELECT COALESCE(SUM(aiu),0) FROM usage_hourly WHERE hour >= ?", (since,)).fetchone()[0]

        def top_for(since: str) -> list:
            return conn.execute(
                "SELECT sid, SUM(aiu) a FROM usage_hourly WHERE hour >= ? GROUP BY sid ORDER BY a DESC LIMIT ?", (since, top_n)
            ).fetchall()

        tokens: dict[str, int] = {}
        for (tj,) in conn.execute("SELECT tokens_json FROM usage_files WHERE tokens_json IS NOT NULL"):
            for k, v in json.loads(tj).items():
                tokens[k] = tokens.get(k, 0) + int(v)
        mixes = {sid: json.loads(tj) for sid, tj in conn.execute("SELECT sid, tokens_json FROM usage_files WHERE tokens_json IS NOT NULL")}

        def split_for(since: str, fallback: dict[str, int]) -> list[dict[str, Any]]:
            """Estimated token-type split for a range: each session's cost-weighted token mix
            (latest shutdown) applied to the AIU it spent in the range. Sessions without a
            shutdown event use the all-time mix."""
            acc = {k: 0.0 for k in RATES_PER_M}
            for sid, aiu in conn.execute("SELECT sid, SUM(aiu) FROM usage_hourly WHERE hour >= ? GROUP BY sid", (since,)):
                mix = {x["type"]: x["pct"] for x in _split(mixes.get(sid) or fallback)}
                for k in acc:
                    acc[k] += aiu * mix.get(k, 0.0) / 100.0
            total_aiu = sum(acc.values()) or 1.0
            return [{"type": k, "aiu": round(v, 1), "pct": round(100 * v / total_aiu, 1), "tokens": int(v / RATES_PER_M[k] * 1e6)} for k, v in acc.items()]

        by_day = dict(conn.execute("SELECT substr(hour,1,10), SUM(aiu) FROM usage_hourly WHERE hour >= ? GROUP BY 1", (day(29),)))
        daily = [{"day": day(n), "aiu": round(by_day.get(day(n), 0.0), 1)} for n in range(29, -1, -1)]
        by_hour = dict(conn.execute("SELECT substr(hour,12,2), SUM(aiu) FROM usage_hourly WHERE hour >= ? GROUP BY 1", (day(0),)))
        hourly = [{"day": f"{day(0)} {h:02d}:00", "aiu": round(by_hour.get(f"{h:02d}", 0.0), 1)} for h in range(24)]
        ranges = {
            "1": {"series": hourly, "unit": "hour", "top": top_for(day(0)), "token_split": split_for(day(0), tokens)},
            "7": {"series": daily[-7:], "unit": "day", "top": top_for(day(6)), "token_split": split_for(day(6), tokens)},
            "30": {"series": daily, "unit": "day", "top": top_for(day(29)), "token_split": split_for(day(29), tokens)},
        }
        sessions_count = conn.execute("SELECT COUNT(DISTINCT sid) FROM usage_hourly").fetchone()[0]
        t_today, t7, t30 = total(day(0)), total(day(6)), total(day(29))
    finally:
        conn.close()
    return {
        "totals": {"today": round(t_today, 1), "d7": round(t7, 1), "d30": round(t30, 1)},
        "daily": daily,
        "burn": {"avg7": round(t7 / 7, 1), "avg30": round(t30 / 30, 1), "projection30": round(t7 / 7 * 30)},
        "ranges": {k: {"series": v["series"], "unit": v["unit"], "token_split": v["token_split"], "top": [{"session_id": sid, "aiu": round(a, 1)} for sid, a in v["top"]]} for k, v in ranges.items()},
        "token_split": _split(tokens),
        "sessions_count": sessions_count,
        "heavy_threshold": HEAVY_SESSION_AIU,
    }


def session_detail(session_id: str | None, conversation_id: str | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """AIU + context + cache state for one session (selected in the UI)."""
    out: dict[str, Any] = {"aiu": None, "context": None, "cache": None}
    if session_id:
        conn = _connect(db_path)
        try:
            out["aiu"] = round(conn.execute("SELECT COALESCE(SUM(aiu),0) FROM usage_hourly WHERE sid=?", (session_id,)).fetchone()[0], 1)
            row = conn.execute("SELECT context_json, tokens_json FROM usage_files WHERE sid=?", (session_id,)).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            out["context"] = json.loads(row[0])
            out["context"]["source"] = "last_shutdown"
        if row and row[1]:
            out["token_split"] = _split(json.loads(row[1]))
    live = _live.get(conversation_id or "") or {}
    if live.get("context"):
        out["context"] = {**(out["context"] or {}), **live["context"], "source": "live"}
    if live.get("cache_expires_at"):
        out["cache"] = {"expires_at": live["cache_expires_at"], "ttl_seconds": live.get("cache_ttl", 300)}
    elif session_id:
        out["cache"] = _cache_from_log(session_id)
    return out


def _cache_from_log(session_id: str) -> dict[str, Any] | None:
    """Last checkpoint's modelCacheState from the tail of events.jsonl (covers restarts)."""
    path = SESSION_STATE_DIR / session_id / "events.jsonl"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 200_000))
            tail = fh.read().splitlines()
    except OSError:
        return None
    for raw in reversed(tail):
        if b'"session.usage_checkpoint"' in raw:
            try:
                states = json.loads(raw)["data"].get("modelCacheState") or []
            except Exception:
                return None
            if states:
                return {"expires_at": states[0].get("cacheExpiresAt"), "ttl_seconds": states[0].get("cacheTtlSeconds", 300)}
            return None
    return None


def note_live_event(conversation_id: str, data: Any) -> None:
    """Called for every SDK event of a live bot session; keeps only usage-related fields."""
    try:
        name = type(data).__name__
        if name == "SessionUsageInfoData":
            _live.setdefault(conversation_id, {})["context"] = {
                "currentTokens": data.current_tokens, "tokenLimit": data.token_limit,
                "systemTokens": data.system_tokens, "toolDefinitionsTokens": data.tool_definitions_tokens,
                "conversationTokens": data.conversation_tokens,
            }
        elif name == "AssistantUsageData" and getattr(data, "cache_expires_at", None):
            _live.setdefault(conversation_id, {})["cache_expires_at"] = data.cache_expires_at.isoformat().replace("+00:00", "Z")
    except Exception:
        log.exception("note_live_event failed")


def quota_ttl() -> float:
    try:
        return max(60.0, float(config.value("agent.quota_ttl_s") or QUOTA_TTL_SECONDS))
    except ValueError:
        return float(QUOTA_TTL_SECONDS)


def _http_quota() -> dict[str, Any] | None:
    """Fresh quota straight from GitHub (copilot_internal/user): the only source with the REAL reset date
    (``quota_reset_date_utc``); the CLI's account.getQuota ``resetDate`` is just the snapshot timestamp."""
    import urllib.request
    token = config.copilot_github_token()
    if not token:
        return None
    req = urllib.request.Request(COPILOT_USER_URL, headers={"Authorization": f"token {token}", "User-Agent": "dop-agent"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    snap = (d.get("quota_snapshots") or {}).get("premium_interactions")
    if not snap:
        return None
    ent = snap.get("entitlement") or 0
    rem = snap.get("remaining") if snap.get("remaining") is not None else snap.get("quota_remaining")
    used = max(0, ent - rem) if rem is not None else 0
    reset = d.get("quota_reset_date_utc") or d.get("quota_reset_date")
    return {
        "entitlement": ent, "used": used, "unlimited": bool(snap.get("unlimited")),
        "remaining_pct": snap.get("percent_remaining"), "used_pct": round(100 * used / ent, 1) if ent else None,
        "overage": snap.get("overage_count", 0), "reset_at": reset, "source": "github",
    }


async def _rpc_quota(client: Any) -> dict[str, Any] | None:
    from copilot.generated.rpc import AccountGetQuotaRequest
    res = await client.rpc.account.get_quota(AccountGetQuotaRequest())
    snap = (res.to_dict().get("quotaSnapshots") or {}).get("premium_interactions")
    if not snap:
        return None
    ent, used = snap["entitlementRequests"], snap["usedRequests"]
    return {
        "entitlement": ent, "used": used, "unlimited": snap["isUnlimitedEntitlement"],
        "remaining_pct": snap["remainingPercentage"], "used_pct": round(100 * used / ent, 1) if ent else None,
        "overage": snap["overage"], "reset_at": None, "source": "cli-rpc",
    }


async def fetch_quota(client: Any, force: bool = False) -> dict[str, Any] | None:
    """premium_interactions quota, lazily refreshed every :func:`quota_ttl` (default 1h; ``force`` = on demand,
    at most every QUOTA_FORCE_MIN_SECONDS). A failed refresh KEEPS the last good value (never caches None) and
    sets ``error``; the result carries ``fetched_at`` (epoch s) so the UI can show its age."""
    import asyncio
    now = time.monotonic()
    c = _quota_cache
    age = now - c["at"] if c["at"] else None
    if c["value"] is not None and age is not None:
        limit = QUOTA_FORCE_MIN_SECONDS if force else quota_ttl()
        if age < limit:
            return _quota_view(limited=force)
    if c["tried"] and now - c["tried"] < QUOTA_RETRY_SECONDS and not force:
        return _quota_view()
    c["tried"] = now
    value, err = None, None
    try:
        value = await asyncio.to_thread(_http_quota)
    except Exception as e:  # network/auth: fall back to the CLI RPC below
        err = f"github: {e}"
        log.warning("copilot_internal/user quota fetch failed: %s", e)
    if value is None and client is not None:
        try:
            value = await _rpc_quota(client)
            if value is not None:
                err = None
        except Exception as e:
            err = f"rpc: {e}"
            log.exception("get_quota failed")
    if value is not None:
        c.update(at=now, value=value, fetched_at=time.time(), error=err, tried=0.0)
    else:
        c["error"] = err or "quota unavailable"
    return _quota_view()


def quota_next_due() -> float:
    """Seconds until the cached quota is due for a refresh (>= 60 so a failing source is not hammered)."""
    at = _quota_cache["at"]
    if not at or _quota_cache["value"] is None:
        return QUOTA_RETRY_SECONDS
    return max(60.0, quota_ttl() - (time.monotonic() - at))


def _quota_view(limited: bool = False) -> dict[str, Any] | None:
    """``limited`` = an on-demand refresh was refused by the force gap (UI shows a hint)."""
    c = _quota_cache
    if c["value"] is None:
        return None
    return {**c["value"], "fetched_at": c["fetched_at"], "error": c["error"], "ttl_seconds": quota_ttl(), "refresh_limited": limited}
