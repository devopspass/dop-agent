#!/usr/bin/env python3
"""Microsoft Teams watcher (read-only). Script-watch exit codes: 0 = nothing new (silent),
200 = changed (notice printed), 1 = error (token/CDP tab missing, API failure).

Modes (first arg):
  messages    new chat messages, optionally filtered. Flags: --chat ID (default: any chat),
              --from NAME (repeatable, substring of display name / mri), --match REGEX,
              --mentions-me, --files, --exclude-self, --kind text|any
              => covers: new message in chat, from user, matching text, @mention,
                 new file shared (--files).
  transcript  new call-transcript (--what transcript) or recording (--what recording) message
              in --chat ID (default: any chat).
  meeting-started   online meeting whose start passed and end not yet reached (once per event).
  meeting-soon      online meeting starting within --minutes N (default 5), once per event.
  meeting-created   new calendar event appeared in the next --days D (default 30).
  meeting-changed   event cancelled or rescheduled in the next --days D.

State: $WATCH_STATE_DIR/teams-<mode>.json (default ~/.dop-agent/watch-state/teams-default).
The first run only records a baseline (exit 0). Cursors only move forward, so nothing is
reported twice. Bodies are HTML-stripped and cut to --max-chars (default 200); nothing else
is logged. 429/5xx are treated as transient (silent exit 0, warning on stderr).
Tokens come from the signed-in Teams tab via CDP (drivers/ms_teams/cli/auth.py); --cdp-url/--account
are forwarded as TEAMS_CDP_URL/TEAMS_ACCOUNT for that module.
"""
import argparse
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
RC_CHANGED = 200
TEXT_TYPES = {"Text", "RichText/Html"}
GRAPH = "https://graph.microsoft.com/v1.0"
KEEP_IDS = 500


class Transient(Exception):
    pass


def _teams_modules():
    sys.path.insert(0, str(CLI_DIR))
    import auth, chatsvc, http_util  # noqa: E401
    return auth, chatsvc, http_util


def _wrap(fn):
    try:
        return fn()
    except RuntimeError as exc:
        if re.search(r"\b(429|500|502|503|504)\b", str(exc)):
            raise Transient(str(exc)) from exc
        raise


def fetch_conversations():
    _, chatsvc, _ = _teams_modules()
    return _wrap(lambda: chatsvc.list_conversations(50))


def fetch_messages(chat_id, limit=20):
    _, chatsvc, _ = _teams_modules()
    return _wrap(lambda: chatsvc.get_messages(chat_id, limit))


def my_oid():
    auth, _, _ = _teams_modules()
    return auth.decode_jwt(auth.tab_token(auth.RESOURCE_IC3)).get("oid", "")


def fetch_events(days_back, days_ahead):
    auth, _, http_util = _teams_modules()
    now = datetime.now(timezone.utc)
    s = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"{GRAPH}/me/calendarView?startDateTime={s}&endDateTime={e}&$top=200"
           "&$select=id,subject,start,end,isOnlineMeeting,isCancelled,onlineMeeting"
           "&$orderby=start/dateTime")
    status, data = http_util.call(auth.tab_token(auth.RESOURCE_GRAPH, "Calendars"), "GET", url,
                                  extra_headers={"Prefer": 'outlook.timezone="UTC"'})
    if status in (429, 500, 502, 503, 504):
        raise Transient(f"graph {status}")
    if status != 200:
        raise RuntimeError(f"calendar_view failed: {status}")
    return data.get("value", [])


def now_utc():
    return datetime.now(timezone.utc)


# ---- helpers -------------------------------------------------------------

def strip_html(text, limit):
    t = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def parse_dt(s):
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", ""))
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def msg_id(m):
    try:
        return int(m.get("id"))
    except (TypeError, ValueError):
        return 0


def chat_title(c):
    return (c.get("threadProperties") or {}).get("topic") or c.get("id", "?")


def state_path(mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent/watch-state/teams-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"teams-{mode}.json"


def load_state(mode):
    try:
        return json.loads(state_path(mode).read_text())
    except (OSError, ValueError):
        return None


def save_state(mode, st):
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def candidate_chats(args, st):
    """Chats whose last message changed since the previous poll (one list call, then reads
    only the changed ones). Returns [(chat_id, title)], updated chat->last-id map."""
    if args.chat:
        return [(args.chat, args.chat)], st.get("last", {})
    convs = fetch_conversations()
    last = {c["id"]: str((c.get("lastMessage") or {}).get("id", "")) for c in convs}
    prev = st.get("last", {})
    titles = {c["id"]: chat_title(c) for c in convs}
    return [(cid, titles[cid]) for cid in last if last[cid] != prev.get(cid)], last


# ---- message modes -------------------------------------------------------

def msg_matches(m, args, me):
    if args.kind == "text" and m.get("messagetype") not in TEXT_TYPES:
        return False
    sender = f"{m.get('imdisplayname') or ''} {m.get('from') or ''}".lower()
    if args.exclude_self and me and me in sender:
        return False
    if args.sender and not any(s.lower() in sender for s in args.sender):
        return False
    content = m.get("content") or ""
    if args.match and not re.search(args.match, strip_html(content, 100000), re.I):
        return False
    if args.mentions_me:
        blob = f"{content} {json.dumps((m.get('properties') or {}).get('mentions', ''))}"
        if not me or me not in blob:
            return False
    if args.files and not (m.get("properties") or {}).get("files") and "<attachment" not in content:
        return False
    return True


def run_messages(args, st, first):
    me = my_oid() if (args.mentions_me or args.exclude_self) else ""
    base = st.setdefault("baseline_id", int(now_utc().timestamp() * 1000))
    cursor = st.setdefault("cursor", {})
    chats, st["last"] = candidate_chats(args, st)
    if first:
        # Baseline: remember "now" and each chat's last id; report nothing.
        return []
    out = []
    for cid, title in chats:
        floor = cursor.get(cid, base)
        msgs = sorted(fetch_messages(cid, args.limit), key=msg_id)
        newest = floor
        for m in msgs:
            i = msg_id(m)
            if i <= floor:
                continue
            newest = max(newest, i)
            if msg_matches(m, args, me):
                who = m.get("imdisplayname") or "?"
                out.append(f"[{title}] {who} @ {m.get('composetime', '')[:19]}: "
                           f"{strip_html(m.get('content'), args.max_chars)}  (chat={cid} id={m.get('id')})")
        cursor[cid] = newest
    return out


def run_transcript(args, st, first):
    chatsvc_find = _teams_modules()[1].find_call_transcripts
    seen = st.setdefault("seen", [])
    chats, st["last"] = candidate_chats(args, st)
    out = []
    for cid, title in chats:
        msgs = fetch_messages(cid, args.limit)
        if args.what == "transcript":
            items = chatsvc_find(msgs)
        else:
            items = [{"title": strip_html(m.get("content"), 80), "time": m.get("composetime", ""),
                      "url": f"rec:{m.get('id')}"} for m in msgs
                     if m.get("messagetype") == "RichText/Media_CallRecording"
                     and 'type="amsVideo"' in (m.get("content") or "")]
        for it in items:
            if it["url"] in seen:
                continue
            seen.append(it["url"])
            if not first:
                out.append(f"[{title}] {args.what} ready: {it['title']} ({it['time'][:19]}) chat={cid}"
                           + (f" url={it['url']}" if args.what == "transcript" else ""))
    st["seen"] = seen[-KEEP_IDS:]
    return out


# ---- calendar modes ------------------------------------------------------

def _ev_line(e, extra=""):
    return f"{e.get('subject') or '(no subject)'} @ {(e.get('start') or {}).get('dateTime', '')[:16]}Z {extra}".strip()


def run_calendar(args, st, first):
    mode, now = args.mode, now_utc()
    events = fetch_events(1, args.days)
    out = []
    if mode in ("meeting-started", "meeting-soon"):
        done = st.setdefault("notified", {})
        for e in events:
            if not e.get("isOnlineMeeting") or e.get("isCancelled"):
                continue
            s, en = parse_dt(e["start"]["dateTime"]), parse_dt(e["end"]["dateTime"])
            if not s or not en:
                continue
            key = f"{e['id']}|{e['start']['dateTime']}"
            if mode == "meeting-started":
                hit = s <= now < en
            else:
                hit = now < s <= now + timedelta(minutes=args.minutes)
            if hit and key not in done:
                done[key] = now.isoformat()
                if not first:
                    join = (e.get("onlineMeeting") or {}).get("joinUrl", "")
                    out.append(("STARTED " if mode == "meeting-started" else "SOON ") + _ev_line(e, join))
        # Drop entries that left the window so state stays bounded.
        live = {f"{e['id']}|{e['start']['dateTime']}" for e in events}
        st["notified"] = {k: v for k, v in done.items() if k in live}
    else:
        known = st.setdefault("known", {})
        cur = {e["id"]: {"start": e["start"]["dateTime"], "cancelled": bool(e.get("isCancelled"))}
               for e in events}
        by_id = {e["id"]: e for e in events}
        for eid, info in cur.items():
            old = known.get(eid)
            if mode == "meeting-created" and old is None:
                out.append("NEW " + _ev_line(by_id[eid]))
            elif mode == "meeting-changed" and old is not None:
                if info["cancelled"] and not old["cancelled"]:
                    out.append("CANCELLED " + _ev_line(by_id[eid]))
                elif info["start"] != old["start"] and not info["cancelled"]:
                    out.append(f"RESCHEDULED {_ev_line(by_id[eid])} (was {old['start'][:16]}Z)")
        st["known"] = cur
        if first:
            out = []
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["messages", "transcript", "meeting-started", "meeting-soon",
                                     "meeting-created", "meeting-changed"])
    ap.add_argument("--chat", help="chatId (19:...@thread.v2 / 19:..._..@unq.gbl.spaces); default any chat")
    ap.add_argument("--from", dest="sender", action="append", help="sender substring, repeatable, user-specified")
    ap.add_argument("--match", help="regex on message text (case-insensitive)")
    ap.add_argument("--mentions-me", action="store_true")
    ap.add_argument("--files", action="store_true", help="only messages with attachments")
    ap.add_argument("--exclude-self", action="store_true")
    ap.add_argument("--kind", choices=["text", "any"], default="text")
    ap.add_argument("--what", choices=["transcript", "recording"], default="transcript")
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=20, help="messages read per changed chat")
    ap.add_argument("--max-chars", type=int, default=200)
    ap.add_argument("--cdp-url")
    ap.add_argument("--account")
    args = ap.parse_args(argv)
    if args.match:
        try:
            re.compile(args.match)
        except re.error as exc:
            print(f"invalid --match regex: {exc}", file=sys.stderr)
            return 1
    if args.cdp_url:
        os.environ["TEAMS_CDP_URL"] = args.cdp_url
    if args.account:
        os.environ["TEAMS_ACCOUNT"] = args.account
    st = load_state(args.mode)
    first = st is None
    st = st or {}
    try:
        if args.mode == "messages":
            lines = run_messages(args, st, first)
        elif args.mode == "transcript":
            lines = run_transcript(args, st, first)
        else:
            lines = run_calendar(args, st, first)
    except Transient as exc:
        print(f"transient Teams API error, skipping this tick: {exc}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"teams_watch {args.mode} failed: {exc} (is the Teams tab open with CDP "
              f"on {os.environ.get('TEAMS_CDP_URL', 'localhost:9222')}?)", file=sys.stderr)
        return 1
    save_state(args.mode, st)
    if lines:
        print("\n".join(lines))
        return RC_CHANGED
    return 0


if __name__ == "__main__":
    sys.exit(main())
