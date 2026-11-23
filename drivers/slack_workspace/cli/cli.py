#!/usr/bin/env python3
"""Slack workspace CLI (another workspace than the bot's own Slack; compact output, bounded scans).

  test | fetch --kind message --ident '{"channel":"C..","ts":"p1700000000123456"}'
  channels [--query Q] [--types public_channel,private_channel,im,mpim] [--limit 100]
  history <channel> [--since 2h] [--limit 20]       channel = ID or #name
  thread <channel> <ts> [--limit 50]
  search '<query>' [--limit 20]                     needs a USER token with search:read
  user <U-ID | @handle | email>        users [--query Q] [--limit 50]
  permalink <channel> <ts>
  post <channel> '<text>' [--thread-ts TS] [--yes]  draft (dry run) unless --yes; blocked when write_mode=none
Instance: --instance / SLACK_WS_INSTANCE. The token is never printed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slackconfig  # noqa: E402
from client import ApiError, Client, clean_ts, human_time, one_line, parse_since  # noqa: E402

MAX_POST = 4000


def cmd_channels(c: Client, a) -> int:
    types = a.types
    if not re.fullmatch(r"(public_channel|private_channel|im|mpim)(,(public_channel|private_channel|im|mpim))*", types):
        raise ApiError("invalid --types: comma list of public_channel,private_channel,im,mpim")
    chans = c.paged("conversations.list", "channels", {"types": types, "exclude_archived": "true"},
                    max(a.limit, 2000) if a.query else a.limit)
    if a.query:
        q = a.query.lower()
        chans = [x for x in chans if q in (x.get("name") or "").lower() or q in ((x.get("topic") or {}).get("value") or "").lower()]
    for ch in chans[: a.limit]:
        kind = "im" if ch.get("is_im") else "mpim" if ch.get("is_mpim") else "private" if ch.get("is_private") else "public"
        label = ch.get("name") or ("DM " + c.user_name(ch.get("user", "")))
        print(f"{ch['id']} {kind:7} #{label} members={ch.get('num_members', '?')} "
              f"{'member' if ch.get('is_member') else ''} {one_line((ch.get('topic') or {}).get('value', ''), 80)}".rstrip())
    return 0


def cmd_history(c: Client, a) -> int:
    ch = c.resolve_channel(a.channel)
    params = {"channel": ch, "limit": min(a.limit, 200)}
    if a.since:
        params["oldest"] = parse_since(a.since)
    msgs = c.call("conversations.history", params).get("messages") or []
    for m in reversed(msgs[: a.limit]):
        print(c.fmt_message(m))
    if not msgs:
        print("(no messages)")
    return 0


def cmd_thread(c: Client, a) -> int:
    ch = c.resolve_channel(a.channel)
    msgs = c.paged("conversations.replies", "messages", {"channel": ch, "ts": clean_ts(a.ts)}, a.limit)
    for m in msgs:
        print(c.fmt_message(m, text_limit=600))
    if not msgs:
        print("(thread not found)")
    return 0


def cmd_search(c: Client, a) -> int:
    d = c.call("search.messages", {"query": a.query, "count": min(a.limit, 100), "sort": "timestamp", "sort_dir": "desc"})
    ms = (d.get("messages") or {}).get("matches") or []
    for m in ms[: a.limit]:
        chn = (m.get("channel") or {}).get("name") or (m.get("channel") or {}).get("id", "?")
        print(f"#{chn} {m.get('ts')} {human_time(m.get('ts'))} {m.get('username', '?')}: {one_line(m.get('text', ''), 300)}"
              f"\n  {m.get('permalink', '')}")
    print(f"({len(ms)} shown of {(d.get('messages') or {}).get('total', '?')})")
    return 0


def _print_user(u: dict) -> None:
    p = u.get("profile") or {}
    print(f"{u.get('id')} @{u.get('name')} | {u.get('real_name') or p.get('real_name', '')} | "
          f"{p.get('title', '')} | {p.get('email', '')} | tz={u.get('tz', '')}"
          f"{' | deleted' if u.get('deleted') else ''}{' | bot' if u.get('is_bot') else ''}")


def cmd_user(c: Client, a) -> int:
    ref = a.ref.strip()
    if re.match(r"^[UW][A-Z0-9]{8,}$", ref):
        _print_user(c.call("users.info", {"user": ref})["user"])
    elif "@" in ref and not ref.startswith("@"):
        _print_user(c.call("users.lookupByEmail", {"email": ref})["user"])
    else:
        h = ref.lstrip("@").lower()
        hits = [u for u in c.paged("users.list", "members", {}, 2000)
                if h in (u.get("name") or "").lower() or h in (u.get("real_name") or "").lower()
                or h in ((u.get("profile") or {}).get("display_name") or "").lower()]
        for u in hits[:10]:
            _print_user(u)
        if not hits:
            print("(no match)")
    return 0


def cmd_users(c: Client, a) -> int:
    q = (a.query or "").lower()
    n = 0
    for u in c.paged("users.list", "members", {}, 2000):
        if u.get("deleted") or (q and q not in json.dumps([u.get("name"), u.get("real_name")]).lower()):
            continue
        _print_user(u)
        n += 1
        if n >= a.limit:
            break
    return 0


def cmd_permalink(c: Client, a) -> int:
    print(c.permalink(c.resolve_channel(a.channel), a.ts))
    return 0


def cmd_post(c: Client, a) -> int:
    if not c.inst.can_write:
        raise ApiError(f"instance {c.inst.name} has write_mode=none: posting is disabled")
    text = a.text
    if not text.strip() or len(text) > MAX_POST:
        raise ApiError(f"text must be 1..{MAX_POST} characters")
    ch = c.resolve_channel(a.channel)
    thread_ts = clean_ts(a.thread_ts) if a.thread_ts else None
    where = f"#{c.channel_name(ch)} ({ch})" + (f" thread {thread_ts}" if thread_ts else "")
    if not a.yes:
        print(f"DRAFT (not sent) workspace={c.inst.name} as {c.me().get('user', '?')} -> {where}\n---\n{text}\n---\n"
              "Show this to the user; re-run the same command with --yes only after they confirm channel and text.")
        return 0
    d = c.call("chat.postMessage", json_body={"channel": ch, "text": text, **({"thread_ts": thread_ts} if thread_ts else {})})
    print(f"SENT to {where}: ts={d.get('ts')} {c.permalink(ch, d['ts']) if d.get('ts') else ''}")
    return 0


def fetch_card(c: Client, kind: str, ident: dict) -> dict:
    if kind != "message":
        raise ApiError(f"unknown kind {kind!r}")
    ch, ts = c.resolve_channel(str(ident.get("channel", ""))), clean_ts(str(ident.get("ts", "")))
    m = (c.call("conversations.history", {"channel": ch, "latest": ts, "oldest": ts, "inclusive": "true", "limit": 1})
         .get("messages") or [])
    if not m:
        raise ApiError("message not found (deleted, a thread reply, or the token has no access to the channel)")
    m = m[0]
    name = c.channel_name(ch)
    return {"title": f"#{name}: {one_line(m.get('text', ''), 90)}", "url": c.permalink(ch, ts),
            "fields": {"channel": f"#{name}", "author": c.user_name(m.get("user", "")), "posted": human_time(ts),
                       "replies": str(m.get("reply_count", 0)), "text": one_line(m.get("text", ""), 300)}}


def run_test(name: str | None) -> dict:
    steps = []

    def step(n, ok, detail=""):
        steps.append({"step": n, "ok": ok, "detail": detail})
        return ok

    def done(msg):
        return {"ok": all(s["ok"] for s in steps), "message": msg, "steps": steps}

    try:
        inst = slackconfig.load_instance(name)
        step("config", True, f"instance {inst.name} -> {inst.base_url} (api {inst.api}, write_mode {inst.options.get('write_mode') or 'confirm'})")
    except slackconfig.InstanceError as exc:
        step("config", False, str(exc))
        return done(str(exc))
    c = Client(inst)
    try:
        me = c.me()
        step("auth", True, f"{'bot' if me['bot'] else 'user'} {me['user']} in team {me['team']}")
    except ApiError as exc:
        step("auth", False, slackconfig.redact(str(exc)))
        return done(f"auth.test failed: {slackconfig.redact(str(exc))}")
    try:
        n = len(c.call("conversations.list", {"limit": 20, "exclude_archived": "true"}).get("channels") or [])
        step("channels", True, f"{n} channel(s) visible (first page)")
    except ApiError as exc:
        step("channels", False, slackconfig.redact(str(exc)))
    try:
        c.call("search.messages", {"query": "a", "count": 1})
        step("search", True, "search.messages allowed")
    except ApiError as exc:
        step("search", False, slackconfig.redact(str(exc)) + " (optional: needs a user token with search:read)")
    ok_core = all(s["ok"] for s in steps if s["step"] != "search")
    res = done("connected" if ok_core else "some required checks failed")
    res["ok"] = ok_core
    return res


def build():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instance", help="slack_workspace integration instance (env SLACK_WS_INSTANCE)")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser
    add("test")
    s = add("fetch"); s.add_argument("--kind", required=True); s.add_argument("--ident", required=True)
    s = add("channels"); s.add_argument("--query"); s.add_argument("--types", default="public_channel,private_channel")
    s.add_argument("--limit", type=int, default=100)
    s = add("history"); s.add_argument("channel"); s.add_argument("--since"); s.add_argument("--limit", type=int, default=20)
    s = add("thread"); s.add_argument("channel"); s.add_argument("ts"); s.add_argument("--limit", type=int, default=50)
    s = add("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=20)
    s = add("user"); s.add_argument("ref")
    s = add("users"); s.add_argument("--query"); s.add_argument("--limit", type=int, default=50)
    s = add("permalink"); s.add_argument("channel"); s.add_argument("ts")
    s = add("post"); s.add_argument("channel"); s.add_argument("text"); s.add_argument("--thread-ts"); s.add_argument("--yes", action="store_true")
    return p


def main(argv=None) -> int:
    a = build().parse_args(argv)
    if hasattr(a, "limit"):
        a.limit = min(max(a.limit, 1), 2000)
    try:
        if a.cmd == "test":
            print(json.dumps(run_test(a.instance)))
            return 0
        c = Client(slackconfig.load_instance(a.instance))
        if a.cmd == "fetch":
            print(json.dumps(fetch_card(c, a.kind, json.loads(a.ident))))
            return 0
        fn = {"channels": cmd_channels, "history": cmd_history, "thread": cmd_thread, "search": cmd_search,
              "user": cmd_user, "users": cmd_users, "permalink": cmd_permalink, "post": cmd_post}[a.cmd]
        return fn(c, a) or 0
    except (ApiError, slackconfig.InstanceError, ValueError) as exc:
        print(f"ERROR: {slackconfig.redact(str(exc))[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
