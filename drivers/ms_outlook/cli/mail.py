"""Mail commands (Outlook REST via the Teams-tab token)."""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import api
import util
from util import q, addr, trunc, table, die

WELL_KNOWN = {"inbox", "sentitems", "drafts", "archive", "deleteditems", "junkemail", "outbox"}
ALIASES = {"sent": "sentitems", "deleted": "deleteditems", "trash": "deleteditems", "junk": "junkemail",
           "spam": "junkemail"}
LIST_SELECT = "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview"


def parse_since(s):
    m = re.fullmatch(r"(\d+)([dh])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return datetime.now(timezone.utc) - (timedelta(days=n) if u == "d" else timedelta(hours=n))
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        die(f"bad --since {s!r}: use 7d, 12h or ISO date")
    return d if d.tzinfo else d.astimezone()


def iso_utc(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def folder_id(acct, name):
    n = ALIASES.get(name.lower(), name.lower())
    if n in WELL_KNOWN:
        return n
    d = api.request("/me/mailFolders?$top=100&$select=id,displayName")
    for f in d.get("value", []):
        if f["displayName"].lower() == name.lower():
            return f["id"]
    die(f"folder {name!r} not found (top-level folders: " + ", ".join(f["displayName"] for f in d["value"]) + ")")


def _local(ts):
    if not ts:
        return ""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


def _row(i, m):
    return {"ref": f"m{i}", "date": _local(m.get("receivedDateTime")),
            "unread": "" if m.get("isRead") else "*", "att": "📎" if m.get("hasAttachments") else "",
            "from": addr(m.get("from")), "subject": m.get("subject"),
            "preview": trunc(m.get("bodyPreview"), 80).split("... [")[0]}


def cmd_list(args, acct):
    base = "/me/messages" if not args.folder else f"/me/mailFolders/{q(folder_id(acct, args.folder))}/messages"
    use_search = bool(args.query or args.subject)
    if use_search:
        kql = []
        if args.query:
            kql.append(args.query)
        if args.subject:
            kql.append(f"subject:{args.subject}")
        if args.sender:
            kql.append(f"from:{args.sender}")
        if args.since:
            kql.append(f"received>={parse_since(args.since).strftime('%Y-%m-%d')}")
        kql = " ".join(kql).replace('"', "'")
        url = f'{base}?$search="{q(kql)}"&$top=100&$select={LIST_SELECT}'
    else:
        flt = []
        if args.unread:
            flt.append("isRead eq false")
        if args.sender:
            flt.append(f"from/emailAddress/address eq '{args.sender}'")
        if args.since:
            flt.append(f"receivedDateTime ge {iso_utc(parse_since(args.since))}")
        fs = f"&$filter={q(' and '.join(flt))}" if flt else ""
        url = f"{base}?$top={min(args.top, 100)}&$orderby=receivedDateTime desc{fs}&$select={LIST_SELECT}"
        url = url.replace(" desc", "%20desc")
    items = api.get_all(url, 100 if use_search else args.top)
    if use_search:
        if args.unread:
            items = [m for m in items if not m.get("isRead")]
        items.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    items = items[:args.top]
    util.save_refs(acct, "m", [m["id"] for m in items])
    table([_row(i, m) for i, m in enumerate(items, 1)], ["ref", "date", "unread", "att", "from", "subject"] +
          (["preview"] if args.preview else []), args.json)


def cmd_read(args, acct):
    mid = util.resolve(acct, args.id)
    m = api.request(f"/me/messages/{q(mid)}?$select=subject,from,toRecipients,ccRecipients,"
                           "receivedDateTime,isRead,hasAttachments,body,conversationId,internetMessageId")
    body = m.get("body", {})
    text = util.html_to_text(body["content"]) if str(body.get("contentType")).lower() == "html" else body.get("content", "")
    if not args.full:
        text = trunc(text, args.chars)
    print(f"From: {addr(m.get('from'))}\nTo: {', '.join(addr(r) for r in m.get('toRecipients', []))}")
    if m.get("ccRecipients"):
        print(f"Cc: {', '.join(addr(r) for r in m['ccRecipients'])}")
    print(f"Date: {m.get('receivedDateTime')}\nSubject: {m.get('subject')}\n"
          f"Attachments: {'yes' if m.get('hasAttachments') else 'no'}\n\n{text}")


def _recips(s):
    return [{"emailAddress": {"address": a.strip()}} for a in (s or "").split(",") if a.strip()]


def _body_text(args):
    if args.body_file:
        return sys.stdin.read() if args.body_file == "-" else open(args.body_file, encoding="utf-8").read()
    return args.body or ""


def _message(args):
    msg = {"subject": args.subject, "toRecipients": _recips(args.to),
           "body": {"contentType": "HTML" if args.html else "Text", "content": _body_text(args)}}
    if args.cc:
        msg["ccRecipients"] = _recips(args.cc)
    if args.bcc:
        msg["bccRecipients"] = _recips(args.bcc)
    return msg


def _preview(msg):
    print("DRY RUN (nothing created or sent)")
    print(f"To: {', '.join(addr(r) for r in msg.get('toRecipients', []))}")
    for k in ("ccRecipients", "bccRecipients"):
        if msg.get(k):
            print(f"{k[:3].title()}: {', '.join(addr(r) for r in msg[k])}")
    print(f"Subject: {msg.get('subject')}\n\n{trunc(msg['body']['content'], 600)}")


def cmd_send(args, acct):
    if not args.to:
        die("--to is required")
    msg = _message(args)
    if args.dry_run:
        return _preview(msg)
    if args.yes:
        api.request("/me/sendMail", "post", {"message": msg, "saveToSentItems": True})
        print(f"SENT to {args.to}")
    else:
        d = api.request("/me/messages", "post", msg)
        util.save_refs(acct, "d", [d["id"]])
        print(f"DRAFT created (not sent), ref d1. Send with: mail-send-draft d1 --yes\nTo: {args.to}\nSubject: {args.subject}")


def cmd_send_draft(args, acct):
    mid = util.resolve(acct, args.id)
    d = api.request(f"/me/messages/{q(mid)}?$select=subject,toRecipients,ccRecipients,isDraft")
    if not d.get("isDraft"):
        die("message is not a draft")
    rc = ", ".join(addr(r) for r in d.get("toRecipients", []) + d.get("ccRecipients", []))
    if not args.yes:
        print(f"NOT sent (add --yes). Draft '{d.get('subject')}' -> {rc}")
        return
    api.request(f"/me/messages/{q(mid)}/send", "post")
    print(f"SENT '{d.get('subject')}' -> {rc}")


def _reply_like(args, acct, kind):
    mid = util.resolve(acct, args.id)
    body = {"comment": _body_text(args)}
    if kind == "forward":
        if not args.to:
            die("--to is required")
        body["toRecipients"] = _recips(args.to)
    verb = {"reply": "replyAll" if getattr(args, "all", False) else "reply", "forward": "forward"}[kind]
    if args.dry_run:
        print(f"DRY RUN: would {verb} to message {args.id} with comment:\n{trunc(body['comment'], 600)}")
        if "toRecipients" in body:
            print("To:", args.to)
        return
    if args.yes:
        api.request(f"/me/messages/{q(mid)}/{verb}", "post", body)
        print(f"SENT {verb}")
    else:
        cr = "create" + verb[0].upper() + verb[1:]
        d = api.request(f"/me/messages/{q(mid)}/{cr}", "post", body)
        util.save_refs(acct, "d", [d["id"]])
        print(f"DRAFT {verb} created (not sent), ref d1. Send with: send-draft d1 --yes")


def cmd_reply(args, acct):
    _reply_like(args, acct, "reply")


def cmd_forward(args, acct):
    _reply_like(args, acct, "forward")


def cmd_mark(args, acct):
    flag = args.state == "read"
    for ref in args.ids:
        api.request(f"/me/messages/{q(util.resolve(acct, ref))}", "patch", {"isRead": flag})
    print(f"marked {len(args.ids)} message(s) {args.state}")


def cmd_move(args, acct):
    dest = folder_id(acct, args.folder)
    for ref in args.ids:
        api.request(f"/me/messages/{q(util.resolve(acct, ref))}/move", "post", {"destinationId": dest})
    print(f"moved {len(args.ids)} message(s) to {args.folder}")


def cmd_folders(args, acct):
    d = api.request("/me/mailFolders?$top=100&$select=displayName,totalItemCount,unreadItemCount")
    table([{"name": f["displayName"], "total": f["totalItemCount"], "unread": f["unreadItemCount"]}
           for f in d["value"]], ["name", "total", "unread"], args.json)


def cmd_attachments(args, acct):
    mid = util.resolve(acct, args.id)
    d = api.request(f"/me/messages/{q(mid)}/attachments?$select=id,name,size,contentType,isInline")
    atts = d.get("value", [])
    if args.download is None:
        table([{"n": i, "name": a["name"], "size": a["size"], "type": a["contentType"],
                "inline": a["isInline"]} for i, a in enumerate(atts, 1)], ["n", "name", "size", "type", "inline"],
              args.json)
        return
    os.makedirs(args.download, exist_ok=True)
    for i, a in enumerate(atts, 1):
        if args.only and args.only not in (str(i), a["name"]):
            continue
        path = os.path.join(args.download, os.path.basename(a["name"]))
        api.request(f"/me/messages/{q(mid)}/attachments/{q(a['id'])}/$value", file_path=path)
        print(f"saved {path} ({a['size']} bytes)")
