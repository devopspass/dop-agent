#!/usr/bin/env python3
"""
session_review.py — data-gathering helper for the "dop-agent-tooling-review" skill.

Aggregates Copilot CLI session logs (~/.copilot/session-store.db +
~/.copilot/session-state/<id>/events.jsonl) for a given repo, since the last
recorded review, into tool-call counts and categorized bash-command patterns.
This script only crunches data — deciding what's worth automating and writing
the human-facing findings is the agent's job (see the SKILL.md).

State (last-review bookmark + history) is kept per-repo in:
    ~/.copilot/tooling-review-state.json

Subcommands:
    scan     Print aggregated data for the window since the last review
              (or a bootstrap window on first run) as JSON.
    record   Append a new review entry (findings/actions/backlog, supplied as
              JSON on stdin) to the state file, closing out the window.
    history  Print past review entries for the repo (for context/comparison).

Examples:
    python3 dop-agent/session_review.py scan
    python3 dop-agent/session_review.py scan --repo /path/to/repo --bootstrap-days 21
    echo '{"findings": [...], "actions_taken": [...], "open_suggestions": [...]}' \\
        | python3 dop-agent/session_review.py record --review-session-id <uuid>
    python3 dop-agent/session_review.py history
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

STATE_PATH = os.path.expanduser("~/.copilot/tooling-review-state.json")
SESSION_DB = os.path.expanduser("~/.copilot/session-store.db")
SESSION_STATE_DIR = os.path.expanduser("~/.copilot/session-state")

DEFAULT_BOOTSTRAP_DAYS = 21
MAX_EXAMPLES_PER_CATEGORY = 6


# --------------------------------------------------------------------------
# Repo / state helpers
# --------------------------------------------------------------------------

def get_repo_root(path=None):
    path = path or os.getcwd()
    try:
        out = subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return os.path.abspath(path)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def get_history(state, repo_root):
    return state.get(repo_root, {}).get("reviews", [])


def last_backlog(history):
    """Open suggestions carried forward from the most recent review, if any."""
    if not history:
        return []
    return history[-1].get("open_suggestions", [])


# --------------------------------------------------------------------------
# Session log reading
# --------------------------------------------------------------------------

def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_sessions(repo_root, since_iso, until_iso):
    if not os.path.exists(SESSION_DB):
        return []
    db = sqlite3.connect(SESSION_DB)
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, created_at FROM sessions
        WHERE (cwd = ? OR cwd LIKE ? ESCAPE '\\')
          AND created_at >= ? AND created_at <= ?
        ORDER BY created_at ASC
        """,
        (repo_root, _like_escape(repo_root) + "%", since_iso, until_iso),
    )
    rows = cur.fetchall()
    db.close()
    return rows


def iter_tool_events(session_id):
    path = os.path.join(SESSION_STATE_DIR, session_id, "events.jsonl")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "tool.execution_start":
                continue
            data = ev.get("data", {})
            yield {
                "toolName": data.get("toolName", ""),
                "arguments": data.get("arguments", {}) or {},
                "timestamp": ev.get("timestamp"),
            }


# --------------------------------------------------------------------------
# Bash command categorization
# --------------------------------------------------------------------------

CATEGORY_RULES = [
    # (category, matcher(cmd, low) -> bool)
    # Specific / anchored checks first; generic substring checks last, so a
    # command that merely *mentions* e.g. "aws-core-infra" in a path doesn't
    # get miscategorized as an AWS CLI invocation.
    ("api-curl", lambda c, low: ("/api/v4/" in low and "curl" in low)),
    ("teams-script-usage", lambda c, low: "teams_chats.py" in c),
    ("mr-render-usage", lambda c, low: "mr_render" in low or "mr-render" in low),
    ("vpn-cdp", lambda c, low: "global-protect" in low or "globalprotect" in low or "remote-debugging-port" in low),
    ("bare-sleep", lambda c, low: bool(re.match(r"^\s*sleep\s+\d", c))),
    ("ps-wait-loop", lambda c, low: "while ps -p" in c),
    ("inline-python", lambda c, low: bool(re.search(r"python3?\s+(-c\b|-\s*<<|<<\s*['\"]?(PY|EOF|PYEOF))", c))),
    ("bash-profile-boilerplate", lambda c, low: "bash_profile" in low or "zshrc" in low),
    ("git-cli", lambda c, low: bool(re.match(r"^\s*(cd [^&]*&&\s*)?git(\s|$)", c))),
    ("jira-confluence-curl", lambda c, low: "curl" in low and "atlassian" in low),
    ("grafana-curl", lambda c, low: "curl" in low and "grafana" in low),
    ("ssh-or-c-instance", lambda c, low: bool(re.match(r"^\s*c\s+(dev|tst|stg|prd|snb|mig)", c))
     or bool(re.search(r"(^|[\n;]|&&)\s*ssh\s", c))),
    ("aws-cli", lambda c, low: bool(re.search(r"(^|[\n;]|&&)\s*(\S*/)?aws(-vault)?\s+[a-z][a-z-]+", c, re.MULTILINE))),
    ("sqlite-analysis", lambda c, low: "sqlite3" in low or "session-store.db" in low),
    ("pkg-install", lambda c, low: "pip3 install" in low or "npm install" in low or "go install" in low),
    ("find-fs", lambda c, low: bool(re.match(r"^\s*find\s", c))),
    ("grep-search", lambda c, low: bool(re.match(r"^\s*(grep|rg)\s", c))),
    ("other-curl", lambda c, low: "curl" in low),
]


def categorize_bash(cmd):
    low = cmd.lower()
    for category, matcher in CATEGORY_RULES:
        try:
            if matcher(cmd, low):
                return category
        except re.error:
            continue
    return "misc"


def normalize_signature(cmd):
    """Collapse numeric IDs / tmp paths so near-identical repeated commands cluster together."""
    s = re.sub(r"\d+", "N", cmd)
    s = re.sub(r"/tmp/\S+", "/tmp/X", s)
    s = re.sub(r"/var/folders/\S+", "/var/folders/X", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def aggregate(session_ids):
    tool_counts = Counter()
    bash_categories = Counter()
    bash_examples = defaultdict(list)
    bash_signatures = Counter()
    signature_examples = {}
    sessions_with_events = 0

    for sid in session_ids:
        had_any = False
        for ev in iter_tool_events(sid):
            had_any = True
            tn = ev["toolName"]
            tool_counts[tn] += 1
            if tn == "bash":
                cmd = (ev["arguments"] or {}).get("command", "")
                if not cmd.strip():
                    continue
                cat = categorize_bash(cmd)
                bash_categories[cat] += 1
                if len(bash_examples[cat]) < MAX_EXAMPLES_PER_CATEGORY:
                    bash_examples[cat].append({"session": sid[:8], "command": cmd[:220]})
                sig = normalize_signature(cmd)
                bash_signatures[sig] += 1
                signature_examples.setdefault(sig, cmd[:200])
        if had_any:
            sessions_with_events += 1

    top_repeated = [
        {"count": n, "signature": sig, "example": signature_examples[sig]}
        for sig, n in bash_signatures.most_common(20)
        if n >= 3
    ]

    return {
        "sessions_with_events": sessions_with_events,
        "tool_counts": tool_counts.most_common(60),
        "bash_categories": bash_categories.most_common(),
        "bash_examples": {k: v for k, v in bash_examples.items()},
        "top_repeated_bash_signatures": top_repeated,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_scan(args):
    repo_root = get_repo_root(args.repo)
    state = load_state()
    history = get_history(state, repo_root)

    now = datetime.now(timezone.utc)
    if history:
        since = history[-1]["window_end"]
    else:
        since = (now - timedelta(days=args.bootstrap_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = query_sessions(repo_root, since, until)
    session_ids = [r[0] for r in rows]
    agg = aggregate(session_ids)

    report = {
        "repo_root": repo_root,
        "window": {"start": since, "end": until, "is_bootstrap": not history},
        "sessions_found": len(session_ids),
        "session_ids": session_ids,
        "previous_review": {
            "completed_at": history[-1]["completed_at"] if history else None,
            "open_suggestions": last_backlog(history),
        } if history else None,
        **agg,
    }
    print(json.dumps(report, indent=2))


def cmd_record(args):
    repo_root = get_repo_root(args.repo)
    state = load_state()
    history = get_history(state, repo_root)

    now = datetime.now(timezone.utc)
    if history:
        window_start = history[-1]["window_end"]
    else:
        window_start = (now - timedelta(days=args.bootstrap_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 1

    rows = query_sessions(repo_root, window_start, window_end)

    entry = {
        "review_session_id": args.review_session_id,
        "completed_at": window_end,
        "window_start": window_start,
        "window_end": window_end,
        "sessions_reviewed": len(rows),
        "findings": payload.get("findings", []),
        "actions_taken": payload.get("actions_taken", []),
        "open_suggestions": payload.get("open_suggestions", []),
    }

    state.setdefault(repo_root, {"reviews": []})
    state[repo_root]["reviews"].append(entry)
    save_state(state)

    print(f"RESULT: RECORDED — review for {repo_root} saved "
          f"({len(rows)} sessions, window {window_start} .. {window_end})")
    return 0


def cmd_history(args):
    repo_root = get_repo_root(args.repo)
    state = load_state()
    history = get_history(state, repo_root)
    if not history:
        print(f"RESULT: NO_HISTORY — no prior reviews recorded for {repo_root}")
        return 0
    print(json.dumps({"repo_root": repo_root, "reviews": history}, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Aggregate Copilot CLI session logs for the tooling-review skill.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Aggregate sessions since the last recorded review")
    p_scan.add_argument("--repo", help="Repo path (default: current directory's git root)")
    p_scan.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS,
                         help=f"Window size in days on first-ever run (default: {DEFAULT_BOOTSTRAP_DAYS})")
    p_scan.set_defaults(func=cmd_scan)

    p_record = sub.add_parser("record", help="Record this review's findings/actions (reads JSON from stdin)")
    p_record.add_argument("--repo", help="Repo path (default: current directory's git root)")
    p_record.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS)
    p_record.add_argument("--review-session-id", required=True,
                           help="This Copilot CLI session's own id (from the session_context folder path)")
    p_record.set_defaults(func=cmd_record)

    p_history = sub.add_parser("history", help="Print past review entries for a repo")
    p_history.add_argument("--repo", help="Repo path (default: current directory's git root)")
    p_history.set_defaults(func=cmd_history)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
