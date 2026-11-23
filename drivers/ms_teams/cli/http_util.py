"""Tiny HTTP helper shared by the API modules. stdlib only."""
import json
import urllib.error
import urllib.request


def call(token: str, method: str, url: str, body=None, extra_headers: dict = None):
    """Return (status, parsed_json_or_text)."""
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        text = resp.read().decode()
        return resp.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except Exception:
            return e.code, text
