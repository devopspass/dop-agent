"""
Session-group helpers that sit above message_store: status rollups, the
group overview aggregate (powering the Web UI's group overview page), and the
"suggest a group for a new session" matcher.

Gotchas:
- Artifacts (merge-request URLs / ticket keys) are regex-extracted from the conversation
  *messages*, not from the Copilot CLI's session_refs table - that table
  only ever contains commit SHAs in practice, so it can't answer "which MRs
  did this workstream touch".
- Everything here is derived data; nothing is persisted. Overview is
  computed on demand (one pass over the group's messages) so it can never go
  stale against the sessions it summarizes.
- Pure functions take the already-built session payload (``_session_payload``
  in web_server.py, which overlays live ``running``/``errored`` flags) so
  this module has no dependency on web_server and stays unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

import message_store

_MR_RE = re.compile(r"https?://[^\s)\]>\"']*?/-/merge_requests/\d+")
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
_URL_JIRA_RE = re.compile(r"atlassian\.net/browse/([A-Z][A-Z0-9]{1,9}-\d{1,6})")
# Status priority for rollups: most attention-worthy first.
STATUS_ORDER = ["errored", "running", "unread", "idle"]
_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "into", "new", "session", "please", "make", "add", "fix"}


def session_status(item: dict[str, Any]) -> str:
    """errored > running > unread (new activity since last read) > idle."""
    if item.get("errored"):
        return "errored"
    if item.get("running"):
        return "running"
    updated, last_read = item.get("updated_at") or "", item.get("last_read_at") or ""
    if item.get("last_role") == "assistant" and updated and (not last_read or updated > last_read):
        return "unread"
    return "idle"


def rollup(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {s: 0 for s in STATUS_ORDER}
    for item in sessions:
        if item.get("archived"):
            continue
        counts[session_status(item)] += 1
    top = next((s for s in STATUS_ORDER if counts[s]), "idle")
    return {"status": top, "counts": counts}


def decorate_sessions_with_groups(sessions: list[dict[str, Any]], groups: list[dict[str, Any]]) -> None:
    by_id = {g["id"]: g for g in groups}
    for item in sessions:
        g = by_id.get(item.get("group_id"))
        item["group_title"] = g["title"] if g else None
        item["group_color"] = g["color"] if g else None
        item["group_emoji"] = g["emoji"] if g else None
        item["status"] = session_status(item)


def groups_payload(sessions: list[dict[str, Any]], include_archived: bool = False) -> list[dict[str, Any]]:
    groups = message_store.list_groups(include_archived=include_archived)
    for g in groups:
        members = [s for s in sessions if s.get("group_id") == g["id"]]
        g.update(rollup(members))
        last = max((s.get("updated_at") or "" for s in members), default="")
        g["last_activity"] = last or g["updated_at"]
    return groups


def extract_artifacts(texts: list[str]) -> dict[str, list[str]]:
    mrs: dict[str, None] = {}
    tickets: dict[str, None] = {}
    for text in texts:
        for m in _MR_RE.findall(text):
            mrs[m] = None
        for m in _URL_JIRA_RE.findall(text):
            tickets[m] = None
        for m in _JIRA_RE.findall(text):
            if not m.startswith(("UTF-", "SHA-", "ISO-")):
                tickets[m] = None
    return {"merge_requests": list(mrs), "tickets": list(tickets)}


def overview(group_id: str, sessions: list[dict[str, Any]], watches: list[dict[str, Any]], *, per_session_chars: int = 20000) -> dict[str, Any]:
    group = message_store.get_group(group_id)
    if group is None:
        raise KeyError(group_id)
    members = [s for s in sessions if s.get("group_id") == group_id]
    # Master first, then most recent activity.
    members.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    members.sort(key=lambda s: 0 if s.get("is_group_master") else 1)
    texts: list[str] = []
    timeline: list[dict[str, Any]] = []
    member_ids = {s["conversation_id"] for s in members}
    for s in members:
        timeline.append({"at": s.get("created_at") or s.get("updated_at"), "kind": "session", "conversation_id": s["conversation_id"], "text": f"Session: {s['title']}"})
        for msg in message_store.get_messages(s["conversation_id"]):
            if msg["role"] in ("assistant", "user"):
                texts.append(str(msg["content"])[:per_session_chars])
    timeline = [t for t in timeline if t["at"]]
    timeline.sort(key=lambda t: t["at"], reverse=True)
    attention = [s for s in members if session_status(s) in ("errored", "unread")]
    group_watches = [w for w in watches if w.get("conversation_id") in member_ids and w.get("status") != "done"]
    return {
        "group": group,
        "rollup": rollup(members),
        "master": next((s for s in members if s.get("is_group_master")), None),
        "sessions": members,
        "attention": attention,
        "artifacts": extract_artifacts(texts),
        "watches": group_watches,
        "timeline": timeline[:100],
    }


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", (text or "").lower()) if t not in _STOPWORDS}


def suggest_group(prompt: str, groups: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Suggest an existing group for a new session's first prompt. Cheap
    keyword/ticket matching only (no LLM): a shared ticket key with any
    member session's title wins outright; otherwise the group whose title +
    member titles share the most distinctive tokens, if >= 2 overlap."""
    if not prompt or not groups:
        return None
    prompt_tickets = set(_JIRA_RE.findall(prompt))
    ptokens = _tokens(prompt)
    best, best_score = None, 0
    for g in groups:
        if g.get("archived"):
            continue
        titles = [g["title"]] + [s.get("title") or "" for s in sessions if s.get("group_id") == g["id"]]
        score = 0
        gtickets: set[str] = set()
        for t in titles:
            gtickets |= set(_JIRA_RE.findall(t))
        if prompt_tickets & gtickets:
            score = 100
        else:
            gtok: set[str] = set()
            for t in titles:
                gtok |= _tokens(t)
            overlap = ptokens & gtok
            score = len(overlap) if len(overlap) >= 2 else 0
        if score > best_score:
            best, best_score = g, score
    if best is None:
        return None
    return {"group_id": best["id"], "title": best["title"], "color": best.get("color"), "score": best_score}
