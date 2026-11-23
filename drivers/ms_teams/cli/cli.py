#!/usr/bin/env python3
"""Teams CLI — chats, meetings, calendar, users. Read/write per command,
see README.md for scope and auth details.

Run as: python3 cli.py <command> ...
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import auth
import chatsvc
import furl
import graph
import mtoptions


def cmd_chats_list(args):
    convs = chatsvc.list_conversations()
    rows = [{"id": c["id"], "topic": c.get("threadProperties", {}).get("topic"),
             "last": (c.get("lastMessage") or {}).get("composetime")} for c in convs]
    print(json.dumps(rows if args.json else {"count": len(rows), "conversations": rows}, indent=2))


def cmd_chats_read(args):
    msgs = chatsvc.get_messages(args.chat_id, args.limit)
    out = [{"from": m.get("imdisplayname") or m.get("from"), "time": m.get("composetime"),
            "content": m.get("content")} for m in msgs]
    print(json.dumps(out, indent=2))


def cmd_transcript(args):
    found = chatsvc.find_call_transcripts(chatsvc.get_messages(args.chat_id, args.limit))
    if not found:
        raise RuntimeError("no call transcript found in this chat (not recorded/transcribed, or not ready yet)")
    if args.index >= len(found):
        raise RuntimeError(f"only {len(found)} transcript(s) found")
    chosen = found[args.index]
    print(f"transcript: {chosen['title']} ({chosen['time']}); {len(found)} found", file=sys.stderr)
    vtt = chatsvc.get_transcript(chosen["url"])
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(vtt)
        print(json.dumps({"ok": True, "bytes": len(vtt), "path": args.save, "title": chosen["title"],
                          "time": chosen["time"]}, indent=2))
    else:
        print(vtt)


def cmd_send(args):
    chat_id = "48:notes" if args.chat_id == "me" else args.chat_id
    result = chatsvc.send_message(chat_id, args.text)
    print(json.dumps({"ok": True, "arrivalTime": result.get("OriginalArrivalTime")}, indent=2))


def cmd_cal_list(args):
    now = datetime.now(timezone.utc)
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=args.days)).isoformat().replace("+00:00", "Z")
    events = graph.calendar_view(start, end)
    rows = [{"subject": e["subject"], "start": e["start"]["dateTime"],
             "isOnlineMeeting": e["isOnlineMeeting"],
             "joinUrl": (e.get("onlineMeeting") or {}).get("joinUrl")} for e in events]
    print(json.dumps(rows, indent=2))


def cmd_cal_create(args):
    event = graph.create_event(args.title, args.start, args.end, teams_meeting=args.teams)
    print(json.dumps({"id": event["id"], "joinUrl": (event.get("onlineMeeting") or {}).get("joinUrl")}, indent=2))


def cmd_cal_delete(args):
    graph.delete_event(args.event_id)
    print(json.dumps({"ok": True}, indent=2))


def cmd_meeting_audit(args):
    now = datetime.now(timezone.utc)
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=args.days)).isoformat().replace("+00:00", "Z")
    events = graph.calendar_view(start, end, top=100)
    seen = set()
    for e in events:
        if not e["isOnlineMeeting"]:
            continue
        join_url = (e.get("onlineMeeting") or {}).get("joinUrl")
        if not join_url or join_url in seen:
            continue
        seen.add(join_url)
        try:
            opts = mtoptions.get_options(join_url)
            mode = opts.get("AutoRecordAndTranscribeMode")
        except Exception as exc:
            mode = f"<error: {exc}>"
        print(json.dumps({"subject": e["subject"], "organizer": e["organizer"]["emailAddress"]["address"],
                           "recordAndTranscribe": mode}))


def cmd_meeting_set_recording(args):
    result = mtoptions.set_recording_mode(args.join_url, args.mode)
    print(json.dumps({"ok": result.get("success", False)}, indent=2))


def cmd_user_info(args):
    print(json.dumps(graph.user_info(args.email), indent=2))


def cmd_user_avatar(args):
    data = graph.user_photo(args.email)
    if args.save:
        path = args.save
        with open(path, "wb") as f:
            f.write(data)
    else:  # private fresh file: a predictable /tmp name could be a pre-planted symlink
        fd, path = tempfile.mkstemp(prefix="teams-avatar-", suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    print(json.dumps({"ok": True, "bytes": len(data), "path": path}, indent=2))


def cmd_teams_list(args):
    print(json.dumps(graph.joined_teams(), indent=2))


def cmd_fetch(args):
    print(json.dumps(furl.fetch(args.kind, json.loads(args.ident))))


def cmd_test(args):
    print(json.dumps(furl.run_test()))


def build_parser():
    p = argparse.ArgumentParser(prog="teams-cli")
    p.add_argument("--cdp-url", help="Browser CDP endpoint (env TEAMS_CDP_URL, default http://localhost:9222)")
    p.add_argument("--account", help="UPN/email of the signed-in Teams account to use (env TEAMS_ACCOUNT)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("chats-list"); s.add_argument("--json", action="store_true"); s.set_defaults(func=cmd_chats_list)
    s = sub.add_parser("chats-read"); s.add_argument("chat_id"); s.add_argument("--limit", type=int, default=5); s.set_defaults(func=cmd_chats_read)
    s = sub.add_parser("transcript"); s.add_argument("chat_id"); s.add_argument("--limit", type=int, default=50); s.add_argument("--index", type=int, default=0, help="0 = most recent"); s.add_argument("--save"); s.set_defaults(func=cmd_transcript)
    s = sub.add_parser("send"); s.add_argument("chat_id"); s.add_argument("text"); s.set_defaults(func=cmd_send)

    s = sub.add_parser("cal-list"); s.add_argument("--days", type=int, default=7); s.set_defaults(func=cmd_cal_list)
    s = sub.add_parser("cal-create"); s.add_argument("title"); s.add_argument("start"); s.add_argument("end"); s.add_argument("--teams", action="store_true"); s.set_defaults(func=cmd_cal_create)
    s = sub.add_parser("cal-delete"); s.add_argument("event_id"); s.set_defaults(func=cmd_cal_delete)

    s = sub.add_parser("meeting-audit"); s.add_argument("--days", type=int, default=30); s.set_defaults(func=cmd_meeting_audit)
    s = sub.add_parser("meeting-set-recording"); s.add_argument("join_url"); s.add_argument("mode", choices=mtoptions.MODES); s.set_defaults(func=cmd_meeting_set_recording)

    s = sub.add_parser("user-info"); s.add_argument("email"); s.set_defaults(func=cmd_user_info)
    s = sub.add_parser("user-avatar"); s.add_argument("email"); s.add_argument("--save"); s.set_defaults(func=cmd_user_avatar)

    s = sub.add_parser("teams-list"); s.set_defaults(func=cmd_teams_list)

    s = sub.add_parser("fetch", help="furl card for a Teams link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=["chat", "message", "meeting"]); s.add_argument("--ident", required=True)
    s.set_defaults(func=cmd_fetch)
    s = sub.add_parser("test", help="check CDP, Teams tab, token and Graph (JSON; exit 0 even when a step fails)")
    s.set_defaults(func=cmd_test)
    return p


def main():
    args = build_parser().parse_args()
    try:
        auth.configure(args.cdp_url, args.account)
        args.func(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
