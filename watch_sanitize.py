"""Sanitizer for text coming out of watcher scripts before it reaches the model prompt.

Output of an external script is untrusted input: ANSI escapes are stripped and token-like strings
(API keys, bearer tokens, `password=...`) are redacted so a log line can never leak a secret into a session.
"""
from __future__ import annotations

import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")
# (pattern, replacement) pairs; applied first because they are more specific than the generic rules below
_SPECIFIC_RES = [
    (re.compile(r"(?i)(\"[\w.\-]*(?:token|secret|password|passwd|api[_-]?key|authorization|cookie|private[_-]?key)\"\s*:\s*)\"(?:[^\"\\]|\\.)*\""),
     r'\1"[REDACTED]"'),
    (re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*).+$"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:basic|bearer|token|digest)\s+\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
]
_SECRET_RES = [
    re.compile(r"glpat-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(authorization|private-token|password|passwd|secret|token|api[_-]?key)(\s*[:=]\s*)(?:[Bb]earer\s+)?\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
]


def sanitize(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    for rx, repl in _SPECIFIC_RES:
        text = rx.sub(repl, text)
    for rx in _SECRET_RES:
        if rx.groups >= 2:
            text = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        else:
            text = rx.sub("[REDACTED]", text)
    return text
