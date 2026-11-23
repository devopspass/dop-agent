"""chatsvc (IC3) API: list chats, read messages, send messages.

Token: IC3 token from the Teams tab CDP cache.
"""
import random
import re
import urllib.request

import auth
import http_util

BASE = "https://teams.cloud.microsoft/api/chatsvc/emea/v1/users/ME"


def _token() -> str:
    return auth.tab_token(auth.RESOURCE_IC3)


def list_conversations(page_size: int = 50) -> list:
    token = _token()
    status, data = http_util.call(
        token, "GET", f"{BASE}/conversations?view=msnp24Equivalent&pageSize={page_size}"
    )
    if status != 200:
        raise RuntimeError(f"list_conversations failed: {status} {data}")
    return data.get("conversations", [])


def get_messages(conv_id: str, limit: int = 5) -> list:
    token = _token()
    status, data = http_util.call(
        token, "GET",
        f"{BASE}/conversations/{conv_id}/messages?view=msnp24Equivalent&pageSize={limit}",
    )
    if status != 200:
        raise RuntimeError(f"get_messages failed: {status} {data}")
    return data.get("messages", [])


def send_message(conv_id: str, text: str) -> dict:
    token = _token()
    body = {
        "content": text,
        "messagetype": "RichText/Html",
        "contenttype": "text",
        "clientmessageid": str(random.getrandbits(60)),
    }
    status, data = http_util.call(token, "POST", f"{BASE}/conversations/{conv_id}/messages", body)
    if status not in (200, 201):
        raise RuntimeError(f"send_message failed: {status} {data}")
    return data


def find_call_transcripts(messages: list) -> list:
    """Extract amsTranscript (WebVTT) URLs from call-recording messages,
    newest first, deduplicated. Intermediate chunk messages have an empty uri."""
    seen, results = set(), []
    for m in messages:
        if m.get("messagetype") != "RichText/Media_CallRecording":
            continue
        content = m.get("content", "")
        u = re.search(r'type="amsTranscript" uri="([^"]+)"', content)
        if not u or u.group(1) in seen:
            continue
        seen.add(u.group(1))
        t = re.search(r"<Title>([^<]*)</Title>", content)
        results.append({"title": t.group(1) if t else "",
                        "time": m.get("originalarrivaltime") or m.get("composetime", ""),
                        "url": u.group(1)})
    return results


def get_transcript(url: str) -> str:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")
