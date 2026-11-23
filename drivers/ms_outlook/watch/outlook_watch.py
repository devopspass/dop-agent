#!/usr/bin/env python3
"""Microsoft Outlook watcher (read-only). Script-watch exit codes: 0 = nothing new (silent),
200 = changed (notice printed), 1 = error (login expired / missing scope / bad args).

Modes (first arg):
  mail               new messages in --folder (default inbox) passing the filters:
                     --from X (repeatable; "a@b.com" exact, "@b.com" domain, else name substring),
                     --subject REGEX, --match REGEX (subject+preview), --attachment,
                     --importance high, --flagged, --unread, --to-me (I am a direct To recipient, not only cc),
                     --exclude-self (several --from = your own sender list)
  attachments        new messages with attachments; each wake line lists file names, sizes and the message link
                     (same filters as mail; notify only, nothing is downloaded)
  thread             new reply in a conversation: --conversation ID or --reply-to MESSAGE_ID
                     (conversation resolved once and stored); my own messages are ignored
  invite             new meeting invitation received
  meeting-updated    invitation updated / rescheduled (previous time/location present or "Updated:")
  meeting-cancelled  meeting cancellation received
  invite-response    accept/decline/tentative reply to my invite (--response accepted|declined|tentative|any)
  autoreply          out-of-office / automatic reply received (--conversation optional)
  bounce             NDR / undeliverable / mailer-daemon message received
  event-soon         calendar event starting within --minutes N (default 10) or --hours H, once per event
  event-changed      event in the next --days D moved, renamed or cancelled (snapshot diff)
  my-invite-responses  attendee response changes on meetings I organize (snapshot diff)
  unread             inbox unread count crossed above --threshold N (re-arms when it drops back)
  digest             unread digest: count + newest --limit senders/subjects (use a daily cron)
  task-due           flagged mail whose flag due date is within --hours H (default 24), once per message

Account: --account UPN / OUTLOOK_ACCOUNT (the Teams-tab identity); CDP browser: OUTLOOK_CDP_URL (falls back to TEAMS_CDP_URL).
State: $WATCH_STATE_DIR/outlook-<mode>.json (ids/cursors only, never message text). The first
run only records a baseline (exit 0); cursors only move forward, so nothing is reported twice.
Privacy: wake text = time, sender, subject (80 chars), snippet (--max-chars, default 120,
--no-snippet to drop); at most --limit (10) items, the rest as "+N more".
Backend: the ms_outlook driver's cli/api.py (GET only; Outlook REST with the Teams-tab token). 401/403 =>
exit 1 (the watch driver wakes after `retries` and stops; no spam). 429/5xx => silent skip.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

GRAPH = ""  # relative paths: cli/api.py adds the Outlook REST base
CLI_DIR = Path(__file__).resolve().parents[1] / "cli"
KEEP_IDS = 500
RC_CHANGED = 200
AUTOREPLY_RE = r"^(automatic reply|auto(matic)?[- ]?reply|out of office|autoreply|automatische antwort|abwesenheit|réponse automatique)"
BOUNCE_FROM_RE = r"(mailer-daemon|postmaster|microsoftexchange)"
BOUNCE_SUBJECT_RE = r"(undeliverable|delivery status notification|delivery has failed|returned mail|zustellung fehlgeschlagen|nicht zustellbar|mail delivery failed)"
MODES = ["mail", "attachments", "thread", "invite", "meeting-updated", "meeting-cancelled", "invite-response",
         "autoreply", "bounce", "event-soon", "event-changed", "my-invite-responses", "unread", "digest", "task-due"]


class Transient(Exception):
    pass


class AuthError(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(s):
    if not s:
        return None
    s = s.rstrip("Z").split(".")[0]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- config / backend ----------

_configured = set()


def _api(account):
    """The driver's Outlook REST client, configured once per run for this account and CDP browser."""
    sys.path.insert(0, str(CLI_DIR))
    import api
    if account not in _configured:
        api.configure(os.environ.get("OUTLOOK_CDP_URL") or os.environ.get("TEAMS_CDP_URL"),
                      None if account in (None, "default") else account)
        _configured.add(account)
    return api


def graph_get(url, account=None):
    """One read-only GET through the driver's api module. The only network seam (tests patch it)."""
    try:
        d = _api(account).request(url)
    except subprocess.TimeoutExpired as exc:
        raise Transient("request timeout") from exc
    except Exception as exc:  # api.OutlookError, CDP/token failures
        out = str(exc)
        if re.search(r"\b(429|500|502|503|504)\b", out):
            raise Transient(out[:200]) from exc
        if re.search(r"\b(401|403)\b|expired|Unauthorized|not signed in|No unexpired token|CDP endpoint", out, re.I):
            raise AuthError(f"Outlook auth failed for account {account or 'default'} (Teams tab signed out or "
                            "Mail.Read/Calendars.Read missing): " + out[:200]) from exc
        raise RuntimeError(out[:300]) from exc
    if not isinstance(d, dict):
        raise RuntimeError("unexpected Outlook reply")
    return d


# ---------- state ----------

def state_path(mode):
    d = Path(os.environ.get("WATCH_STATE_DIR") or Path.home() / ".dop-agent/watch-state/outlook-default")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"outlook-{mode}.json"


def load_state(mode):
    p = state_path(mode)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(mode, st):
    p = state_path(mode)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


# ---------- formatting ----------

def clip(text, n):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: max(n - 1, 0)] + "…"


def addr(m):
    ea = (m.get("from") or m.get("sender") or {}).get("emailAddress") or {}
    return ea.get("name") or "", (ea.get("address") or "").lower()


def line(m, args):
    name, a = addr(m)
    flags = "".join([" [!]" if m.get("importance") == "high" else "", " [att]" if m.get("hasAttachments") else "",
                     " [flag]" if (m.get("flag") or {}).get("flagStatus", "").lower() == "flagged" else ""])
    s = f"- {(m.get('receivedDateTime') or '')[:16]} {clip(name, 40)} <{a}> | {clip(m.get('subject'), 80)}{flags}"
    if not args.no_snippet and args.max_chars > 0:
        s += f" | {clip(m.get('bodyPreview'), args.max_chars)}"
    return s


def emit(title, lines, args, total=None):
    total = len(lines) if total is None else total
    shown = lines[: args.limit]
    out = [f"{title}: {total}"] + shown
    if total > len(shown):
        out.append(f"+{total - len(shown)} more")
    print("\n".join(out))
    return RC_CHANGED


# ---------- helpers ----------

def sender_matches(m, patterns):
    name, a = addr(m)
    for p in patterns:
        p = p.lower()
        if p.startswith("@"):
            if a.endswith(p):
                return True
        elif "@" in p:
            if a == p:
                return True
        elif p in name.lower() or p in a:
            return True
    return False


def me(args, st):
    if not st.get("me"):
        d = graph_get(f"{GRAPH}/me", args.account)
        # Graph: mail/userPrincipalName; Outlook REST: emailAddress
        st["me"] = sorted({(d.get(k) or "").lower() for k in ("mail", "userPrincipalName", "emailAddress")} - {""})
    return st["me"]


def resolve_folder(args, st):
    f = args.folder
    if re.fullmatch(r"[a-z]+", f) or len(f) > 40:
        return f
    cache = st.setdefault("folders", {})
    if f not in cache:
        d = graph_get(f"{GRAPH}/me/mailFolders?$top=100&$select=id,displayName", args.account)
        hit = [x["id"] for x in d.get("value", []) if x.get("displayName", "").lower() == f.lower()]
        if not hit:
            raise RuntimeError(f"mail folder '{f}' not found (top-level folders only; or pass a folder id)")
        cache[f] = hit[0]
    return cache[f]


def fetch_mail(args, st, folder=None, extra_filter=None):
    """Newest-first messages at/after the cursor (client-side dedup by id)."""
    flt = []
    if extra_filter:
        flt.append(extra_filter)
    if st.get("cursor") and not (extra_filter and "conversationId" in extra_filter):
        flt.append(f"receivedDateTime ge {st['cursor']}")
    q = f"$top=50&$orderby={quote('receivedDateTime desc')}" if not (extra_filter and "conversationId" in extra_filter) else "$top=50"
    if flt:
        q += "&$filter=" + quote(" and ".join(flt), safe="/'")
    base = f"{GRAPH}/me/mailFolders/{folder}/messages" if folder else f"{GRAPH}/me/messages"
    msgs = graph_get(f"{base}?{q}", args.account).get("value", [])
    return sorted(msgs, key=lambda m: m.get("receivedDateTime", ""), reverse=True)


def pick_new(msgs, st):
    seen = set(st.get("ids", []))
    new = [m for m in msgs if m["id"] not in seen]
    if msgs:
        st["cursor"] = max(max(m.get("receivedDateTime", "") for m in msgs), st.get("cursor", ""))
    st["ids"] = ([m["id"] for m in new] + st.get("ids", []))[:KEEP_IDS]
    return new


def meeting_kind(m):
    t = m.get("@odata.type", "")
    mt = (m.get("meetingMessageType") or "")
    mt = mt[:1].lower() + mt[1:]  # Outlook REST returns PascalCase values
    if "eventmessagerequest" in t.lower() and mt in ("meetingRequest", ""):
        upd = (m.get("previousStartDateTime") or m.get("previousEndDateTime") or m.get("previousLocation")
               or re.match(r"(updated|aktualisiert)\b", m.get("subject") or "", re.I))
        return "updated" if upd else "invite"
    if mt == "meetingCancelled":
        return "cancelled"
    if mt in ("meetingAccepted", "meetingTentativelyAccepted", "meetingDeclined"):
        return {"meetingAccepted": "accepted", "meetingTentativelyAccepted": "tentative", "meetingDeclined": "declined"}[mt]
    return ""


def mail_predicate(args, mode, st):
    pats = list(args.sender or [])
    subj = re.compile(args.subject, re.I) if args.subject else None
    text = re.compile(args.match, re.I) if args.match else None
    mine = set(st.get("me", []))

    def pred(m):
        name, a = addr(m)
        if pats and not sender_matches(m, pats):
            return False
        if (args.exclude_self or mode == "thread") and a in mine:
            return False
        if subj and not subj.search(m.get("subject") or ""):
            return False
        if text and not text.search((m.get("subject") or "") + " " + (m.get("bodyPreview") or "")):
            return False
        if args.attachment and not m.get("hasAttachments"):
            return False
        if args.importance and m.get("importance") != args.importance:
            return False
        if args.flagged and (m.get("flag") or {}).get("flagStatus", "").lower() != "flagged":
            return False
        if args.unread and m.get("isRead"):
            return False
        if args.to_me and not mine & {((r.get("emailAddress") or {}).get("address") or "").lower()
                                      for r in m.get("toRecipients") or []}:
            return False
        if mode == "attachments" and not m.get("hasAttachments"):
            return False
        k = meeting_kind(m)
        if mode == "invite" and k != "invite":
            return False
        if mode == "meeting-updated" and k != "updated":
            return False
        if mode == "meeting-cancelled" and k != "cancelled":
            return False
        if mode == "invite-response" and (k not in ("accepted", "tentative", "declined")
                                          or args.response not in ("any", k)):
            return False
        if mode == "autoreply" and not re.search(AUTOREPLY_RE, (m.get("subject") or "").strip(), re.I):
            return False
        if mode == "bounce" and not (re.search(BOUNCE_FROM_RE, a + " " + name, re.I)
                                     and re.search(BOUNCE_SUBJECT_RE, m.get("subject") or "", re.I)
                                     or re.search(BOUNCE_SUBJECT_RE, m.get("subject") or "", re.I) and "nondeliveryreport" in m.get("@odata.type", "").lower()):
            return False
        return True
    return pred


TITLES = {"mail": "New mail", "attachments": "New mail with attachments", "thread": "New reply in thread",
          "invite": "New meeting invite", "meeting-updated": "Meeting updated/rescheduled",
          "meeting-cancelled": "Meeting cancelled", "invite-response": "Response to my invite",
          "autoreply": "Auto-reply / out-of-office", "bounce": "Bounce / undeliverable"}


def attachment_line(m, args):
    """Message line plus file names/sizes and the message link (listing only, nothing is downloaded)."""
    files = graph_get(f"{GRAPH}/me/messages/{quote(m['id'], safe='')}/attachments?$select=name,size,isInline",
                      args.account).get("value", [])
    names = ", ".join(f"{clip(f.get('name'), 40)} ({(f.get('size') or 0) // 1024 or 1} KB)"
                      for f in files if not f.get("isInline")) or "inline only"
    return f"{line(m, args)} | files: {names}" + (f" | {m['webLink']}" if m.get("webLink") else "")


def run_mail(args, st, first):
    mode = args.mode
    me(args, st)
    folder, flt = None, None
    if mode in ("mail", "attachments", "invite", "meeting-updated", "meeting-cancelled", "invite-response", "bounce") or \
            (mode == "autoreply" and not args.conversation):
        folder = resolve_folder(args, st)
    if mode == "thread" or args.conversation:
        conv = args.conversation or st.get("conversation")
        if not conv:
            if not args.reply_to:
                raise ValueError("thread mode needs --conversation ID or --reply-to MESSAGE_ID")
            conv = graph_get(f"{GRAPH}/me/messages/{quote(args.reply_to, safe='')}?$select=conversationId", args.account)["conversationId"]
        st["conversation"] = conv
        flt = f"conversationId eq '{conv}'"
    pred = mail_predicate(args, mode, st)
    new = pick_new(fetch_mail(args, st, folder, flt), st)
    if first:
        return 0
    hits = [m for m in new if pred(m)]
    fmt = attachment_line if mode == "attachments" else line
    return emit(TITLES[mode], [fmt(m, args) for m in hits[:args.limit]], args, total=len(hits)) if hits else 0


def run_unread(args, st, first):
    d = graph_get(f"{GRAPH}/me/mailFolders/inbox?$select=unreadItemCount", args.account)
    n = int(d.get("unreadItemCount", 0))
    above = n > args.threshold
    was = st.get("above", False)
    st["above"] = above
    if first or not above or was:
        return 0
    print(f"Unread inbox count {n} is above threshold {args.threshold}")
    return RC_CHANGED


def run_digest(args, st, first):
    since = iso(now_utc() - timedelta(hours=args.hours))
    flt = quote(f"isRead eq false and receivedDateTime ge {since}", safe="/'")
    msgs = graph_get(f"{GRAPH}/me/mailFolders/inbox/messages?$top=50&$filter={flt}&$orderby={quote('receivedDateTime desc')}",
                     args.account).get("value", [])
    inbox = graph_get(f"{GRAPH}/me/mailFolders/inbox?$select=unreadItemCount", args.account)
    total = inbox.get("unreadItemCount", len(msgs))
    if not msgs and not total:
        return 0
    return emit(f"Unread digest (inbox unread total {total}; last {args.hours}h shown)", [line(m, args) for m in msgs], args)


def run_task_due(args, st, first):
    flt = quote("flag/flagStatus eq 'flagged'", safe="/'")
    msgs = graph_get(f"{GRAPH}/me/messages?$top=50&$filter={flt}", args.account).get("value", [])
    horizon = now_utc() + timedelta(hours=args.hours)
    done = set(st.get("ids", []))
    due = []
    for m in msgs:
        dd = parse_dt(((m.get("flag") or {}).get("dueDateTime") or {}).get("dateTime"))
        if dd and dd <= horizon and m["id"] not in done:
            due.append(m)
    st["ids"] = ([m["id"] for m in due] + list(done))[:KEEP_IDS]
    if first or not due:
        return 0
    return emit("Flagged mail due", [line(m, args) + f" | due {parse_dt(m['flag']['dueDateTime']['dateTime']):%Y-%m-%d %H:%M}Z" for m in due], args)


def view(args, start, end, select):
    q = f"startDateTime={iso(start)}&endDateTime={iso(end)}&$top=100&$select={select}&$orderby=start/dateTime"
    return graph_get(f"{GRAPH}/me/calendarView?{q}", args.account).get("value", [])


def ev_line(e, extra=""):
    st = (e.get("start") or {}).get("dateTime", "")[:16]
    return f"- {st}Z {clip(e.get('subject'), 80)}{extra}"


def run_event_soon(args, st, first):
    now = now_utc()
    minutes = args.window
    events = view(args, now, now + timedelta(minutes=minutes), "id,subject,start,isCancelled")
    done = set(st.get("ids", []))
    fresh = []
    for e in events:
        key = e["id"] + "|" + (e.get("start") or {}).get("dateTime", "")
        if e.get("isCancelled") or key in done:
            continue
        fresh.append((key, e))
    st["ids"] = ([k for k, _ in fresh] + list(done))[:KEEP_IDS]
    if not fresh:
        return 0
    return emit(f"Event starting within {minutes} min", [ev_line(e) for _, e in fresh], args)


def snapshot_events(args, select):
    now = now_utc()
    return view(args, now - timedelta(hours=1), now + timedelta(days=args.days), select)


def run_event_changed(args, st, first):
    cur = {}
    for e in snapshot_events(args, "id,subject,start,end,isCancelled"):
        cur[e["id"]] = [e.get("subject", ""), (e.get("start") or {}).get("dateTime", ""),
                        (e.get("end") or {}).get("dateTime", ""), bool(e.get("isCancelled"))]
    old = st.get("events")
    st["events"] = cur
    if first or old is None:
        return 0
    lines = []
    for eid, v in cur.items():
        o = old.get(eid)
        if o and o != v:
            what = "CANCELLED" if v[3] and not o[3] else "moved" if o[1:3] != v[1:3] else "renamed"
            lines.append(f"- {v[1][:16]}Z {clip(v[0], 80)} ({what}" + (f", was {o[1][:16]}Z" if what == "moved" else "") + ")")
    for eid, o in old.items():
        if eid not in cur and parse_dt(o[1]) and parse_dt(o[1]) > now_utc() and not o[3]:
            lines.append(f"- {o[1][:16]}Z {clip(o[0], 80)} (removed)")
    return emit("Calendar changes", lines, args) if lines else 0


def run_my_invite_responses(args, st, first):
    cur = {}
    for e in snapshot_events(args, "id,subject,start,isOrganizer,attendees"):
        if not e.get("isOrganizer"):
            continue
        for a in e.get("attendees", []):
            ea = a.get("emailAddress") or {}
            cur[f"{e['id']}|{(ea.get('address') or '').lower()}"] = [
                (a.get("status") or {}).get("response", "none"), clip(e.get("subject"), 80),
                (e.get("start") or {}).get("dateTime", "")[:16], ea.get("name") or ea.get("address", "")]
    old = st.get("resp")
    st["resp"] = cur
    if first or old is None:
        return 0
    lines = [f"- {v[2]}Z {v[1]} | {clip(v[3], 40)}: {v[0]}" for k, v in cur.items()
             if k in old and old[k][0] != v[0] and v[0] not in ("none", "notResponded")
             and (args.response in ("any", v[0]))]
    return emit("Responses to my invites", lines, args) if lines else 0


RUNNERS = {"unread": run_unread, "digest": run_digest, "task-due": run_task_due, "event-soon": run_event_soon,
           "event-changed": run_event_changed, "my-invite-responses": run_my_invite_responses}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=MODES)
    ap.add_argument("--account", default=os.environ.get("OUTLOOK_ACCOUNT"))
    ap.add_argument("--folder", default="inbox", help="well-known name (inbox, sentitems, ...), top-level display name or id")
    ap.add_argument("--from", dest="sender", action="append")
    ap.add_argument("--subject")
    ap.add_argument("--match")
    ap.add_argument("--attachment", action="store_true")
    ap.add_argument("--importance", choices=["high", "normal", "low"])
    ap.add_argument("--flagged", action="store_true")
    ap.add_argument("--unread", action="store_true", help="only unread messages")
    ap.add_argument("--to-me", action="store_true", help="I am a direct To recipient (not only cc)")
    ap.add_argument("--exclude-self", action="store_true")
    ap.add_argument("--conversation")
    ap.add_argument("--reply-to", help="id of a message of the thread (e.g. my sent mail)")
    ap.add_argument("--response", choices=["accepted", "declined", "tentative", "any"], default="any")
    ap.add_argument("--minutes", type=int, help="event-soon window in minutes (default 10)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--hours", type=int, help="event-soon window / digest and task-due horizon in hours (default 24; event-soon 10 min)")
    ap.add_argument("--threshold", type=int, default=20)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--max-chars", type=int, default=120)
    ap.add_argument("--no-snippet", action="store_true")
    args = ap.parse_args(argv)
    args.window = args.minutes if args.minutes is not None else args.hours * 60 if args.hours is not None else 10
    args.hours = 24 if args.hours is None else args.hours
    st = load_state(args.mode)
    first = st is None
    st = st or {}
    try:
        rc = RUNNERS[args.mode](args, st, first) if args.mode in RUNNERS else run_mail(args, st, first)
    except Transient as exc:
        print(f"transient, skipped: {exc}", file=sys.stderr)
        return 0
    except (AuthError, ValueError, RuntimeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    save_state(args.mode, st)
    return rc


if __name__ == "__main__":
    sys.exit(main())
