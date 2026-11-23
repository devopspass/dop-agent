#!/usr/bin/env python3
"""Slack workspace watcher (read-only). Script-watch exit codes: 0 = nothing new (silent), 200 = changed (notice
printed), 1 = error (bad args / auth rejected / unknown instance).

Modes (first arg; channels are IDs or #names, `--channel` is repeatable, at most 10):
  messages  new top-level messages in the channels (--keyword K case-insensitive text filter, repeatable;
            --include-self also reports the token owner's own messages)
  mentions  new top-level messages that mention the token's user (`<@me>`)
  thread    new replies in one thread (--channel C --thread-ts TS)
  threads   new replies in threads started during the last --days D (default 3, max 14) in the channel
            (at most 20 threads are checked per run)

Instance: SLACK_WS_INSTANCE (injected from the pinned integration). State: $WATCH_STATE_DIR/slack-<mode>-<key>.json
(timestamps only). The first run records a baseline (exit 0). Rate limit / network errors => silent skip; auth or
permission errors => exit 1. Backend: the driver's cli/client.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
sys.path.insert(0, str(CLI_DIR))

import slackconfig  # noqa: E402
from client import ApiError, Client, clean_ts  # noqa: E402

RC_CHANGED = 200
MAX_CHANNELS = 10
MAX_THREADS = 20
SOFT_ERRORS = ("rate limited", "unreachable")


def state_file(mode: str, key: str) -> Path:
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent" / "watch-state" / "slack_workspace")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"slack-{mode}-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"


def load_state(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_state(p: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".st")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def _newer(a: str, b: str) -> bool:
    return float(a) > float(b)


def run_messages(c: Client, a, mentions: bool) -> int:
    me = c.me()["user_id"]
    mode = "mentions" if mentions else "messages"
    lines: list[str] = []
    for ref in a.channel:
        ch = c.resolve_channel(ref)
        sf = state_file(mode, f"{c.inst.name}|{ch}")
        last = (load_state(sf) or {}).get("last")
        if last is None:
            top = c.call("conversations.history", {"channel": ch, "limit": 1}).get("messages") or []
            save_state(sf, {"last": top[0]["ts"] if top else f"{time.time():.6f}"})
            continue
        msgs = c.call("conversations.history", {"channel": ch, "oldest": last, "limit": 100}).get("messages") or []
        msgs = [m for m in msgs if _newer(m["ts"], last)]
        if msgs:
            save_state(sf, {"last": max((m["ts"] for m in msgs), key=float)})
        name = c.channel_name(ch)
        for m in sorted(msgs, key=lambda x: float(x["ts"])):
            text = m.get("text") or ""
            if mentions and f"<@{me}>" not in text:
                continue
            if not mentions and not a.include_self and m.get("user") == me:
                continue
            if a.keyword and not any(k.lower() in text.lower() for k in a.keyword):
                continue
            lines.append(f"  #{name} {c.fmt_message(m)}")
    if not lines:
        return 0
    print(f"Slack {c.inst.name}: {len(lines)} new {'mention(s)' if mentions else 'message(s)'}")
    for ln in lines[: a.limit]:
        print(ln)
    if len(lines) > a.limit:
        print(f"  ... +{len(lines) - a.limit} more")
    return RC_CHANGED


def run_thread(c: Client, a) -> int:
    ch, ts = c.resolve_channel(a.channel[0]), clean_ts(a.thread_ts)
    sf = state_file("thread", f"{c.inst.name}|{ch}|{ts}")
    last = (load_state(sf) or {}).get("last")
    reps = c.paged("conversations.replies", "messages", {"channel": ch, "ts": ts}, 200)
    newest = max([m["ts"] for m in reps] or [ts], key=float)
    save_state(sf, {"last": newest})
    if last is None:
        return 0
    new = [m for m in reps if m["ts"] != ts and _newer(m["ts"], last)]
    if not new:
        return 0
    print(f"Slack {c.inst.name}: {len(new)} new repl{'y' if len(new) == 1 else 'ies'} in #{c.channel_name(ch)} thread {ts}")
    for m in new[: a.limit]:
        print("  " + c.fmt_message(m))
    return RC_CHANGED


def run_threads(c: Client, a) -> int:
    ch = c.resolve_channel(a.channel[0])
    sf = state_file("threads", f"{c.inst.name}|{ch}")
    old = load_state(sf)
    oldest = f"{time.time() - min(max(a.days, 1), 14) * 86400:.6f}"
    roots = [m for m in c.call("conversations.history", {"channel": ch, "oldest": oldest, "limit": 100}).get("messages") or []
             if m.get("reply_count")]
    cur = {m["ts"]: m.get("latest_reply") or m["ts"] for m in roots}
    save_state(sf, cur)
    if old is None:
        return 0
    lines = []
    name = c.channel_name(ch)
    for root in sorted(roots, key=lambda m: -float(m.get("latest_reply") or m["ts"]))[:MAX_THREADS]:
        prev = old.get(root["ts"], root["ts"])
        if not _newer(cur[root["ts"]], prev):
            continue
        reps = c.paged("conversations.replies", "messages", {"channel": ch, "ts": root["ts"], "oldest": prev}, 50)
        for m in reps:
            if m["ts"] != root["ts"] and _newer(m["ts"], prev):
                lines.append(f"  #{name} thread {root['ts']}: {c.fmt_message(m)}")
    if not lines:
        return 0
    print(f"Slack {c.inst.name}: {len(lines)} new thread repl{'y' if len(lines) == 1 else 'ies'} in #{name}")
    for ln in lines[: a.limit]:
        print(ln)
    return RC_CHANGED


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["messages", "mentions", "thread", "threads"])
    ap.add_argument("--instance", default=os.environ.get("SLACK_WS_INSTANCE"))
    ap.add_argument("--channel", action="append", default=[], help="channel ID or #name (repeatable)")
    ap.add_argument("--thread-ts", help="thread mode: root message ts")
    ap.add_argument("--keyword", action="append", help="messages mode: text filter (repeatable)")
    ap.add_argument("--include-self", action="store_true")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)
    try:
        if not a.channel or len(a.channel) > MAX_CHANNELS:
            raise ApiError(f"pass 1..{MAX_CHANNELS} --channel values")
        if a.mode == "thread" and not a.thread_ts:
            raise ApiError("thread mode needs --thread-ts")
        c = Client(slackconfig.load_instance(a.instance))
        if a.mode in ("messages", "mentions"):
            return run_messages(c, a, a.mode == "mentions")
        return run_thread(c, a) if a.mode == "thread" else run_threads(c, a)
    except slackconfig.InstanceError as exc:
        print(f"ERROR: {slackconfig.redact(str(exc))}", file=sys.stderr)
        return 1
    except ApiError as exc:
        msg = slackconfig.redact(str(exc))
        if any(s in msg for s in SOFT_ERRORS):
            return 0
        print(f"ERROR: {msg[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
