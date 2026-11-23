"""Thin Slack Web API client (requests) plus compact formatting helpers. Talks only to the instance's api_url;
the token is sent as a Bearer header and never printed (errors pass through `redact`)."""
from __future__ import annotations

import re
import time
from datetime import datetime

import requests

from slackconfig import Instance, redact

TIMEOUT = 30
MAX_PAGES = 10
CHANNEL_ID_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")
USER_ID_RE = re.compile(r"^[UWB][A-Z0-9]{8,}$")
TS_RE = re.compile(r"^[0-9]{10}\.[0-9]{6}$")


class ApiError(RuntimeError):
    pass


def clean_ts(ts: str) -> str:
    """Message ts `1700000000.123456`; the permalink form `p1700000000123456` is accepted too."""
    t = (ts or "").strip()
    m = re.fullmatch(r"p?([0-9]{10})\.?([0-9]{6})", t)
    if not m:
        raise ApiError(f"invalid message ts {ts!r}: use 1700000000.123456")
    return f"{m.group(1)}.{m.group(2)}"


def human_time(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "?"


def parse_since(value: str, now: float | None = None) -> str:
    """`90m`, `2h`, `3d` or `YYYY-MM-DD[ HH:MM]` -> epoch string for `oldest`."""
    now = now if now is not None else time.time()
    v = (value or "").strip()
    m = re.fullmatch(r"([0-9]{1,4})([mhd])", v)
    if m:
        return f"{now - int(m.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[m.group(2)]:.6f}"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return f"{datetime.strptime(v, fmt).timestamp():.6f}"
        except ValueError:
            pass
    raise ApiError(f"invalid --since {value!r}: use 90m, 2h, 3d or YYYY-MM-DD[ HH:MM]")


def one_line(text: str, limit: int = 300) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


class Client:
    def __init__(self, inst: Instance):
        self.inst = inst
        self._users: dict[str, str] = {}
        self._me: dict | None = None

    def call(self, method: str, params: dict | None = None, *, json_body: dict | None = None) -> dict:
        if not re.fullmatch(r"[a-z]+\.[A-Za-z.]+", method):
            raise ApiError(f"invalid API method {method!r}")
        headers = {"Authorization": f"Bearer {self.inst.token}"}
        data = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = requests.post(f"{self.inst.api}/{method}", headers=headers, timeout=TIMEOUT,
                              **({"json": json_body} if json_body is not None else {"data": data}))
        except requests.RequestException as exc:
            raise ApiError(f"{self.inst.api} unreachable ({type(exc).__name__}): check api_url / VPN / proxy") from None
        if r.status_code == 429:
            raise ApiError(f"rate limited by Slack; retry after {r.headers.get('Retry-After', '?')}s")
        if r.status_code != 200:
            raise ApiError(f"HTTP {r.status_code} for {method}: {redact(r.text[:200])}")
        try:
            d = r.json()
        except ValueError:
            raise ApiError(f"{method}: non-JSON answer") from None
        if not d.get("ok"):
            err = d.get("error", "unknown_error")
            hint = ""
            if err == "missing_scope":
                hint = f" (needed: {d.get('needed', '?')}; add the scope to the Slack app and reinstall)"
            elif err in ("invalid_auth", "token_revoked", "token_expired", "not_authed", "account_inactive"):
                hint = " (check auth.token validity)"
            elif err in ("channel_not_found", "not_in_channel"):
                hint = " (private/unjoined channel: the token's user/bot must be a member)"
            raise ApiError(f"{method}: {err}{hint}")
        return d

    def paged(self, method: str, key: str, params: dict, limit: int) -> list:
        out: list = []
        cursor = None
        for _ in range(MAX_PAGES):
            d = self.call(method, {**params, "limit": min(200, max(1, limit - len(out))), "cursor": cursor})
            out.extend(d.get(key) or [])
            cursor = (d.get("response_metadata") or {}).get("next_cursor")
            if not cursor or len(out) >= limit:
                break
        return out[:limit]

    def me(self) -> dict:
        if self._me is None:
            d = self.call("auth.test")
            self._me = {"user_id": d.get("user_id", ""), "user": d.get("user", ""), "team": d.get("team", ""),
                        "url": d.get("url", ""), "bot": bool(d.get("bot_id"))}
        return self._me

    def user_name(self, uid: str) -> str:
        if not uid:
            return "?"
        if uid not in self._users:
            if len(self._users) >= 40 or not USER_ID_RE.match(uid):
                return uid
            try:
                u = self.call("users.info", {"user": uid}).get("user") or {}
                p = u.get("profile") or {}
                self._users[uid] = p.get("display_name") or u.get("real_name") or u.get("name") or uid
            except ApiError:
                self._users[uid] = uid
        return self._users[uid]

    def resolve_channel(self, ref: str) -> str:
        """Channel ID as-is; `#name`/`name` is looked up in a bounded conversations.list scan."""
        r = (ref or "").strip()
        if CHANNEL_ID_RE.match(r):
            return r
        name = r.lstrip("#").lower()
        if not re.fullmatch(r"[a-z0-9._\-]{1,80}", name):
            raise ApiError(f"invalid channel {ref!r}: use an ID (C0123ABCD) or #name")
        for ch in self.paged("conversations.list", "channels",
                             {"types": "public_channel,private_channel", "exclude_archived": "true"}, 2000):
            if ch.get("name", "").lower() == name:
                return ch["id"]
        raise ApiError(f"channel #{name} not found among visible channels; pass the channel ID or join it first")

    def channel_name(self, cid: str) -> str:
        try:
            ch = self.call("conversations.info", {"channel": cid}).get("channel") or {}
            return ch.get("name") or ("DM " + self.user_name(ch.get("user", "")) if ch.get("is_im") else cid)
        except ApiError:
            return cid

    def permalink(self, channel: str, ts: str) -> str:
        return self.call("chat.getPermalink", {"channel": channel, "message_ts": clean_ts(ts)}).get("permalink", "")

    def fmt_message(self, m: dict, *, text_limit: int = 300) -> str:
        who = self.user_name(m.get("user", "")) if m.get("user") else (m.get("username") or m.get("bot_id") or "bot")
        text = re.sub(r"<@([UW][A-Z0-9]+)>", lambda x: "@" + self.user_name(x.group(1)), m.get("text") or "")
        extra = ""
        if m.get("reply_count"):
            extra = f" [{m['reply_count']} replies]"
        if m.get("files"):
            extra += f" [{len(m['files'])} file(s)]"
        return f"{m.get('ts')} {human_time(m.get('ts'))} {who}: {one_line(text, text_limit)}{extra}"
