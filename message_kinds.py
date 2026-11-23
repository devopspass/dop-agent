"""Pure, dependency-free classification of stored transcript messages.

Why this exists: watchers post two kinds of ``system`` messages ("New watcher
registered", "<type> watch fired ...") and then the agent replies to every
fire like a normal assistant message, so the Web UI transcript drowned real
messages in watcher noise. Instead of a schema change (which couldn't cover
history), the kind is *derived* from the stored text/role/order at read time
- the watcher notices have stable prefixes (see watchers.py) - so old
sessions get the benefit too.

``classify_turns(turns)`` annotates each transcript turn dict (as built by
web_server._transcript_payload) in place with:

* ``kind``: ``user`` | ``agent`` | ``status`` | ``watch_registered`` |
  ``watch_fired`` | ``watch_reply`` | ``interrupt`` | ``error`` | ``system``
* for ``watch_fired``: ``fire_type`` (ticket, teams, time, session_turn, other),
  ``fire_ok`` (True/False/None) and ``fire_label`` (short human text),
  ``fire_final`` (always False; kept for clients that still read it)
* for ``watch_reply``: ``reply_to`` (id of the last fire it answers) and
  ``reply_class``: ``ack`` (nothing to do - the ACK protocol in
  watchers.FIRE_REPLY_RULES, or a plain "nothing to act on" one-liner),
  ``narr`` (routine short narration) or ``report`` (worth reading: long,
  link/table carrying, failure, or the answer to a final fire)
* for ``watch_reply``/``error``: ``summary`` - one plain-text line for digests

Gotcha: classification of replies is heuristic by design; the Web UI always
offers a "Today" (raw) view, so a misclassification can never hide data.
"""

from __future__ import annotations

import re

_REGISTERED_RE = re.compile(r"new watcher registered", re.IGNORECASE)
_FIRED_RE = re.compile(r"watch(?:er)?(?: #\d+)? fired", re.IGNORECASE)
_RESTART_RE = re.compile(r"bot restarted successfully", re.IGNORECASE)
_INTERRUPT_RE = re.compile(r"interrupt", re.IGNORECASE)
_ERROR_RE = re.compile(r"^\s*(?::warning:|⚠️|:x:|❌)?\s*(?:error|timed out|timeout|model error|something went wrong|copilot request timed out|dop agent request timed out)", re.IGNORECASE)
_ACK_RE = re.compile(r"^\s*ACK\b", re.IGNORECASE)
_LEGACY_ACK_RE = re.compile(
    r"nothing (?:to act on|new|actionable|to do)|no action (?:needed|required)|already (?:handled|addressed|reviewed)"
    r"|echo(?:ed)? (?:of )?(?:my|our) own|own (?:earlier )?(?:push|note|replies)|only repeats|only records"
    r"|routine danger|automatic \"?(?:version|mentioned)",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r"https?://|\]\(")
_TABLE_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
_REPORT_MIN_CHARS = 400


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _plain(text: str, limit: int = 140) -> str:
    s = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*_`#>|]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _fire_info(text: str) -> dict:
    t = text or ""
    head = _first_line(t)
    low = head.lower()
    ok: bool | None = None
    final = False
    if "ticket watch" in low or "jira" in low:
        ftype = "ticket"
    elif "teams watch" in low:
        ftype = "teams"
    elif "scheduled watch" in low:
        ftype = "time"
    elif "session turn watch" in low:
        ftype = "session_turn"
    else:
        ftype = "other"
    if ok is None and (":x:" in head or "❌" in head or re.search(r"`(?:failed|error)`", t)):
        ok = False
    label = re.sub(r"^[^*]*\*[^*]*\*\s*[—-]?\s*", "", head).strip() or head
    label = _plain(label, 90)
    return {"fire_type": ftype, "fire_ok": ok, "fire_final": final, "fire_label": label}


def _reply_class(text: str, fire: dict | None) -> str:
    body = (text or "").strip()
    if _ACK_RE.match(body):
        return "ack"
    failed = bool(fire and fire.get("fire_ok") is False)
    final = bool(fire and fire.get("fire_final"))
    rich = bool(_LINK_RE.search(body) or _TABLE_RE.search(body))
    if failed or len(body) >= _REPORT_MIN_CHARS or (final and rich):
        return "report"
    if len(body) < 220 and _LEGACY_ACK_RE.search(body):
        return "ack"
    if rich and len(body) >= 160:
        return "report"
    return "narr"


def classify_turns(turns: list[dict]) -> list[dict]:
    last_fire: dict | None = None
    for t in turns:
        role = t.get("role")
        text = t.get("text") or ""
        if role == "user":
            t["kind"] = "user"
            last_fire = None
        elif role == "status":
            t["kind"] = "status"
        elif role == "system":
            if _REGISTERED_RE.search(text):
                t["kind"] = "watch_registered"
                t["fire_label"] = _plain(re.sub(r"^[^*]*\*[^*]*\*\s*[—:-]?\s*", "", _first_line(text)), 90)
            elif _FIRED_RE.search(text) or _RESTART_RE.search(text):
                info = _fire_info(text)
                t["kind"] = "watch_fired"
                t.update(info)
                last_fire = dict(info, id=t.get("id"))
            elif _INTERRUPT_RE.search(_first_line(text)):
                t["kind"] = "interrupt"
            elif _ERROR_RE.match(text):
                t["kind"] = "error"
                t["summary"] = _plain(text)
            else:
                t["kind"] = "system"
        else:  # assistant
            if _ERROR_RE.match(text):
                t["kind"] = "error"
                t["summary"] = _plain(text)
            elif last_fire is not None:
                t["kind"] = "watch_reply"
                t["reply_to"] = last_fire["id"]
                t["reply_class"] = _reply_class(text, last_fire)
                t["summary"] = _plain(re.sub(r"^\s*ACK\s*[-—:]?\s*", "", text) or text)
            else:
                t["kind"] = "agent"
    return turns


def is_final_fire_text(text: str) -> bool:
    """True for a watcher notice flagged as the final result the user is waiting for."""
    return bool(_fire_info(text or "")["fire_final"])


def is_thin_reply(text: str) -> bool:
    """A reply too thin to be a final report: empty, an ACK, or a short
    one-liner with no link/table (e.g. "ok, done")."""
    body = (text or "").strip()
    if not body or _ACK_RE.match(body):
        return True
    return len(body) < 300 and not (_LINK_RE.search(body) or _TABLE_RE.search(body))
