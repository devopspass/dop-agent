#!/usr/bin/env python3
"""Outlook CLI (mail, calendar, contacts) over the Outlook REST API with the token of a signed-in Teams web tab.

Global options go before the command: --account <upn> (env OUTLOOK_ACCOUNT), --cdp-url <url> (env OUTLOOK_CDP_URL),
--json.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api  # noqa: E402
import cal  # noqa: E402
import furl  # noqa: E402
import mail  # noqa: E402
import util  # noqa: E402


def cmd_whoami(args, acct):
    me = api.request("/me")
    print(f"as={me.get('emailAddress')} name={me.get('displayName')} token_identity={api.identity()}")


def cmd_accounts(args, acct):
    util.table([{"identity": i, "current": "*" if acct and acct.lower() in i else ""}
                for i in api.identities()], ["identity", "current"], args.json)


def cmd_fetch(args, acct):
    print(json.dumps(furl.fetch(args.kind, json.loads(args.ident))))


def cmd_test(args, acct):
    print(json.dumps(furl.run_test()))


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default=os.environ.get("OUTLOOK_ACCOUNT"),
                   help="UPN/email of the signed-in Teams account to use (env OUTLOOK_ACCOUNT)")
    p.add_argument("--json", action="store_true", help="JSON lines instead of table output")
    p.add_argument("--cdp-url", help="Browser CDP endpoint (env OUTLOOK_CDP_URL/TEAMS_CDP_URL, default http://localhost:9222)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        s.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        return s

    add("whoami", cmd_whoami, "show signed-in identity")
    add("accounts", cmd_accounts, "list accounts signed in at the CDP browser")
    s = add("fetch", cmd_fetch, "furl card for an Outlook link (used by dop-agent)")
    s.add_argument("--kind", required=True, choices=["message", "event"])
    s.add_argument("--ident", required=True)
    add("test", cmd_test, "check CDP, Teams tab, token and the Outlook API (JSON; exit 0 even when a step fails)")

    s = add("mail-list", mail.cmd_list, "list/search messages (refs m1.. are reusable in other commands)")
    s.add_argument("--folder", help="inbox, sent, drafts, archive, deleted, junk or folder name (default: all)")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--from", dest="sender", help="sender address")
    s.add_argument("--subject", help="subject words (uses search)")
    s.add_argument("--query", help="free-text/KQL search")
    s.add_argument("--since", help="7d | 12h | ISO date")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--preview", action="store_true", help="add 80-char body preview column")
    s = add("mail-read", mail.cmd_read, "read a message (body truncated)")
    s.add_argument("id", help="ref (m1) or Graph id")
    s.add_argument("--full", action="store_true")
    s.add_argument("--chars", type=int, default=1500)

    def body_opts(s):
        s.add_argument("--body")
        s.add_argument("--body-file", help="file path or - for stdin")
        s.add_argument("--dry-run", action="store_true", help="print only, create nothing")
        s.add_argument("--yes", action="store_true", help="REALLY send (default: save draft)")

    s = add("mail-send", mail.cmd_send, "new mail: saves a draft; --yes sends")
    s.add_argument("--to", required=True, help="comma-separated")
    s.add_argument("--cc")
    s.add_argument("--bcc")
    s.add_argument("--subject", required=True)
    s.add_argument("--html", action="store_true")
    body_opts(s)
    s = add("mail-send-draft", mail.cmd_send_draft, "send an existing draft (needs --yes)")
    s.add_argument("id")
    s.add_argument("--yes", action="store_true")
    s = add("mail-reply", mail.cmd_reply, "reply: saves a draft; --yes sends")
    s.add_argument("id")
    s.add_argument("--all", action="store_true", help="reply all")
    body_opts(s)
    s = add("mail-forward", mail.cmd_forward, "forward: saves a draft; --yes sends")
    s.add_argument("id")
    s.add_argument("--to", required=True)
    body_opts(s)
    s = add("mail-mark", mail.cmd_mark, "mark read/unread")
    s.add_argument("state", choices=["read", "unread"])
    s.add_argument("ids", nargs="+")
    s = add("mail-move", mail.cmd_move, "move messages to a folder")
    s.add_argument("folder")
    s.add_argument("ids", nargs="+")
    add("mail-folders", mail.cmd_folders, "list mail folders with counts")
    s = add("mail-attachments", mail.cmd_attachments, "list attachments, or download with --download DIR")
    s.add_argument("id")
    s.add_argument("--download", metavar="DIR", help="save attachments into DIR")
    s.add_argument("--only", help="attachment number or name (with --download)")

    s = add("cal-list", cal.cmd_list, "upcoming events")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--start", help="7d/ISO date (default: now)")
    s.add_argument("--top", type=int, default=50)
    s = add("cal-create", cal.cmd_create, "create event (own calendar, no attendees unless --attendees --yes)")
    s.add_argument("subject")
    s.add_argument("start", help="local ISO time, e.g. 2026-12-01T09:00")
    s.add_argument("end")
    s.add_argument("--location")
    s.add_argument("--body")
    s.add_argument("--teams", action="store_true", help="add Teams meeting")
    s.add_argument("--attendees", help="comma-separated; SENDS INVITES, needs --yes")
    s.add_argument("--yes", action="store_true")
    s = add("cal-delete", cal.cmd_delete, "delete event (attendee events need --yes)")
    s.add_argument("id")
    s.add_argument("--yes", action="store_true")
    s = add("cal-free", cal.cmd_free, "find free slots in working hours")
    s.add_argument("--days", type=int, default=5)
    s.add_argument("--start")
    s.add_argument("--duration", type=int, default=30, help="minutes")
    s.add_argument("--from-hour", type=int, default=9)
    s.add_argument("--to-hour", type=int, default=18)
    s.add_argument("--weekends", action="store_true")
    s = add("contacts", cal.cmd_contacts, "list/search personal contacts")
    s.add_argument("query", nargs="?")
    s.add_argument("--top", type=int, default=25)
    return p


def main():
    args = build().parse_args()
    acct = args.account if args.account != "default" else None
    try:
        api.configure(args.cdp_url, acct)
        args.fn(args, acct)
    except api.OutlookError as e:
        util.die(str(e))
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
