#!/usr/bin/env python3
"""
time_analysis.py — data-gathering helper for the "sprint-week-performance-analysis"
skill (Part A: personal time performance).

Scans Copilot CLI session logs (~/.copilot/session-state/<uuid>/events.jsonl) for a
given date range and computes "active time" per session, bucketed by calendar day.
This script only crunches timing data — classifying each session/day as
"tool development" vs "real work" (ticket-tracked delivery) requires reading actual
message content and is left to the agent (see SKILL.md), since it needs judgment.

Active time methodology:
    Sum of gaps between consecutive events in a session, each gap capped at
    MAX_GAP_SECONDS (default 300s / 5 min). This approximates focused/engaged time
    while excluding long idle/waiting periods (overnight, weekend, multi-day
    shutdown/resume gaps of tens of thousands of minutes are excluded by the
    cap instead of being counted as "active").

Usage:
    python3 dop-agent/time_analysis.py --since 2026-08-03
    python3 dop-agent/time_analysis.py --since 2026-08-03 --until 2026-08-10
    python3 dop-agent/time_analysis.py --since 2026-08-03 --sample-messages 3

Output: JSON with per-session active seconds + per-day breakdown + sampled
user.message content per session (to help the agent classify tool-dev vs real-work).
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SESSION_STATE_DIR = os.path.expanduser("~/.copilot/session-state")
MAX_GAP_SECONDS = 300


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def iter_sessions():
    for entry in os.scandir(SESSION_STATE_DIR):
        if not entry.is_dir():
            continue
        events_path = os.path.join(entry.path, "events.jsonl")
        if os.path.isfile(events_path):
            yield entry.name, events_path


def load_events(events_path):
    events = []
    with open(events_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def analyze_session(session_id, events_path, since_dt, until_dt, sample_n):
    events = load_events(events_path)
    if not events:
        return None

    timestamps = []
    user_messages = []
    for e in events:
        ts = e.get("timestamp")
        if not ts:
            continue
        try:
            dt = parse_ts(ts)
        except ValueError:
            continue
        timestamps.append(dt)
        if e.get("type") == "user.message":
            content = (e.get("data") or {}).get("content", "")
            if content:
                user_messages.append((dt, content[:300]))

    if not timestamps:
        return None

    # Only consider sessions with at least one event in the requested window.
    in_window = [t for t in timestamps if since_dt <= t <= until_dt]
    if not in_window:
        return None

    # Active time: sum of gaps between consecutive events, capped, restricted
    # to gaps that fall (at least partially) inside the window.
    day_active = defaultdict(float)
    for i in range(1, len(timestamps)):
        t0, t1 = timestamps[i - 1], timestamps[i]
        if t1 < since_dt or t0 > until_dt:
            continue
        gap = min((t1 - t0).total_seconds(), MAX_GAP_SECONDS)
        if gap <= 0:
            continue
        day_active[t1.date().isoformat()] += gap

    total_active = sum(day_active.values())
    if total_active <= 0:
        return None

    samples = [
        {"time": dt.isoformat(), "content": content}
        for dt, content in user_messages[:sample_n]
    ]

    return {
        "session_id": session_id,
        "events_path": events_path,
        "total_active_seconds": round(total_active, 1),
        "day_active_seconds": {k: round(v, 1) for k, v in day_active.items()},
        "first_seen": timestamps[0].isoformat(),
        "last_seen": timestamps[-1].isoformat(),
        "sample_user_messages": samples,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, help="Start date, YYYY-MM-DD (inclusive, local midnight)")
    ap.add_argument("--until", default=None, help="End date, YYYY-MM-DD (inclusive, defaults to now)")
    ap.add_argument("--sample-messages", type=int, default=5, dest="sample_n",
                     help="Number of sampled user.message contents per session to return for classification")
    args = ap.parse_args()

    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    if args.until:
        until_dt = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc) + timedelta(days=1)
    else:
        until_dt = datetime.now(timezone.utc)

    sessions = []
    for session_id, events_path in iter_sessions():
        try:
            result = analyze_session(session_id, events_path, since_dt, until_dt, args.sample_n)
        except Exception as exc:  # keep scanning even if one session is malformed
            print(f"WARNING: failed to analyze {session_id}: {exc}", file=sys.stderr)
            continue
        if result:
            sessions.append(result)

    sessions.sort(key=lambda s: s["first_seen"])

    day_totals = defaultdict(float)
    for s in sessions:
        for day, secs in s["day_active_seconds"].items():
            day_totals[day] += secs

    output = {
        "since": since_dt.date().isoformat(),
        "until": until_dt.date().isoformat(),
        "session_count": len(sessions),
        "total_active_seconds": round(sum(day_totals.values()), 1),
        "total_active_hours": round(sum(day_totals.values()) / 3600, 2),
        "day_totals_hours": {k: round(v / 3600, 2) for k, v in sorted(day_totals.items())},
        "sessions": sessions,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
