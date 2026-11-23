"""Calendar and contacts commands."""
from datetime import datetime, timedelta, timezone

import api
import util
from mail import iso_utc, parse_since
from util import q, addr, table, die

EV_SELECT = "id,subject,start,end,location,isAllDay,isOnlineMeeting,organizer,attendees,showAs,isCancelled"


def _dt(s):
    """Graph dateTime (UTC, no suffix) -> aware datetime."""
    return datetime.fromisoformat(s["dateTime"][:19]).replace(tzinfo=timezone.utc)


def _view(acct, start, end, top=200):
    url = (f"/me/calendarView?startDateTime={iso_utc(start)}&endDateTime={iso_utc(end)}"
           f"&$top=100&$orderby=start/dateTime&$select={EV_SELECT}")
    return api.get_all(url, top)


def _local(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def cmd_list(args, acct):
    start = parse_since(args.start) if args.start else datetime.now(timezone.utc)
    items = [e for e in _view(acct, start, start + timedelta(days=args.days)) if not e.get("isCancelled")]
    items = items[:args.top]
    util.save_refs(acct, "e", [e["id"] for e in items])
    rows = [{"ref": f"e{i}", "start": _local(_dt(e["start"])), "end": _local(_dt(e["end"]))[11:],
             "subject": e.get("subject"), "where": (e.get("location") or {}).get("displayName"),
             "attendees": len(e.get("attendees", [])), "show": e.get("showAs")}
            for i, e in enumerate(items, 1)]
    table(rows, ["ref", "start", "end", "subject", "where", "attendees", "show"], args.json)


def _parse_local(s):
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.astimezone()


def cmd_create(args, acct):
    start, end = _parse_local(args.start), _parse_local(args.end)
    if end <= start:
        die("end must be after start")
    ev = {"subject": args.subject,
          "start": {"dateTime": iso_utc(start)[:-1], "timeZone": "UTC"},
          "end": {"dateTime": iso_utc(end)[:-1], "timeZone": "UTC"}}
    if args.location:
        ev["location"] = {"displayName": args.location}
    if args.body:
        ev["body"] = {"contentType": "Text", "content": args.body}
    if args.teams:
        ev["isOnlineMeeting"] = True
        ev["onlineMeetingProvider"] = "teamsForBusiness"
    atts = [a.strip() for a in (args.attendees or "").split(",") if a.strip()]
    if atts:
        ev["attendees"] = [{"emailAddress": {"address": a}, "type": "required"} for a in atts]
    if atts and not args.yes:
        print(f"DRY RUN (not created): attendees {', '.join(atts)} would receive invitations; add --yes.\n"
              f"{args.subject} {_local(start)} - {_local(end)[11:]}")
        return
    d = api.request("/me/events", "post", ev)
    util.save_refs(acct, "e", [d["id"]])
    print(f"created ref e1: {args.subject} {_local(start)} - {_local(end)[11:]}"
          + (f"; invitations sent to {len(atts)}" if atts else "; no attendees"))


def cmd_delete(args, acct):
    eid = util.resolve(acct, args.id)
    e = api.request(f"/me/events/{q(eid)}?$select=subject,attendees,isOrganizer,start")
    n = len(e.get("attendees", []))
    if n and e.get("isOrganizer") and not args.yes:
        print(f"NOT deleted: '{e['subject']}' has {n} attendee(s) who would get a cancellation; add --yes.")
        return
    api.request(f"/me/events/{q(eid)}", "delete")
    print(f"deleted '{e['subject']}'")


def cmd_free(args, acct):
    now = datetime.now().astimezone()
    start = _parse_local(args.start) if args.start else now
    busy = []
    end_range = start + timedelta(days=args.days)
    for e in _view(acct, start, end_range, 500):
        if e.get("showAs") in ("free", "workingElsewhere") or e.get("isCancelled"):
            continue
        busy.append((_dt(e["start"]).astimezone(), _dt(e["end"]).astimezone()))
    busy.sort()
    need = timedelta(minutes=args.duration)
    rows = []
    for d in range(args.days + 1):
        day = (start + timedelta(days=d)).replace(hour=0, minute=0, second=0, microsecond=0)
        if day.weekday() >= 5 and not args.weekends:
            continue
        cur = max(day.replace(hour=args.from_hour), start)
        stop = day.replace(hour=args.to_hour)
        for bs, be in busy:
            if be <= cur or bs >= stop:
                continue
            if bs - cur >= need:
                rows.append((cur, bs))
            cur = max(cur, be)
        if stop - cur >= need:
            rows.append((cur, stop))
    table([{"date": s.strftime("%a %Y-%m-%d"), "from": s.strftime("%H:%M"), "to": e.strftime("%H:%M"),
            "minutes": int((e - s).total_seconds() // 60)} for s, e in rows],
          ["date", "from", "to", "minutes"], args.json)


def cmd_contacts(args, acct):
    mob = "mobilePhone1"
    sel = f"id,displayName,emailAddresses,companyName,jobTitle,{mob},businessPhones"
    items = api.get_all(f"/me/contacts?$top=100&$select={sel}", 2000)
    if args.query:
        s = args.query.lower()
        items = [c for c in items if s in (c.get("displayName") or "").lower() or s in (c.get("companyName") or "").lower()
                 or any(s in (e.get("address") or "").lower() for e in c.get("emailAddresses", []))]
    items = items[:args.top]
    table([{"name": c.get("displayName"), "email": ", ".join(e["address"] for e in c.get("emailAddresses", [])),
            "company": c.get("companyName"), "phone": c.get(mob) or ", ".join(c.get("businessPhones", []))}
           for c in items], ["name", "email", "company", "phone"], args.json)
