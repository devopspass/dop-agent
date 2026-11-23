"""Meeting-options API (Teams `mt` service): read/write recording and
transcription settings for a Teams meeting. Token: skype token
from the Teams tab CDP cache.
"""
import re
import urllib.parse

import auth
import http_util

MODES = ["AutoRecordingAndTranscription", "AutoTranscriptionOnly", "AutoRecordingOnly", "Off"]

_JOIN_URL_RE = re.compile(r"(19:meeting_[^/]+@thread\.v2)/(\d+)\?context=(\{.*\})")


def _token() -> str:
    return auth.tab_token(auth.RESOURCE_SKYPE)


def parse_join_url(join_url: str) -> dict:
    """Extract {tenant, organizer_oid, thread, suffix} from a Teams joinUrl."""
    decoded = urllib.parse.unquote(join_url)
    m = _JOIN_URL_RE.search(decoded)
    if not m:
        raise ValueError(f"Could not parse joinUrl: {join_url!r}")
    thread, suffix, ctx_json = m.groups()
    import json
    ctx = json.loads(ctx_json)
    return {
        "tenant": ctx["Tid"],
        "organizer_oid": ctx["Oid"],
        "thread": thread.replace(":", "_", 1),
        "suffix": suffix,
    }


def _options_url(parsed: dict) -> str:
    return (
        "https://teams.microsoft.com/api/mt/part/emea-03/beta/meetings/v1/options/"
        f"{parsed['tenant']}/{parsed['organizer_oid']}/{parsed['thread']}/{parsed['suffix']}?monarch=true"
    )


def get_options(join_url: str) -> dict:
    url = _options_url(parse_join_url(join_url))
    status, data = http_util.call(_token(), "GET", url)
    if status != 200:
        raise RuntimeError(f"get_options failed: {status} {data}")
    return {o["name"]: o.get("currentValue") for o in data.get("options", [])}


def set_recording_mode(join_url: str, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    url = _options_url(parse_join_url(join_url))
    body = {"options": [{"name": "AutoRecordAndTranscribeMode", "type": "List", "currentValue": mode}]}
    status, data = http_util.call(_token(), "POST", url, body)
    if status != 200:
        raise RuntimeError(f"set_recording_mode failed: {status} {data}")
    return data
