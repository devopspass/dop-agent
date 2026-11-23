#!/usr/bin/env python3
"""Copilot CLI preToolUse hook: safe, fail-open rtk rewrite for a tiny allowlist.

Only `git status|log|branch` (no format/patch flags) and plain `ls [paths]`
(no flags), optionally after `cd X &&` and/or piped into head/tail/wc, are
passed to `rtk hook copilot`. Everything else (grep, find, cat, aws, curl,
heredocs, multi-line, c/cf wrappers, ...) is left untouched, because rtk
compaction drops grep matches / ls metadata.

Fail-open: any error, timeout, missing rtk or unexpected output prints
nothing and exits 0 (a hook error would DENY the tool call in Copilot CLI).
Kill switch: env RTK_HOOK_OFF=1 or file ~/.copilot/rtk-hook.off.
Log: ~/.copilot/logs/rtk-hook.log (one line per rewrite/error).
"""
import json
import os
import re
import subprocess
import sys
import time

OFF_FILE = os.path.expanduser("~/.copilot/rtk-hook.off")
LOG = os.path.expanduser("~/.copilot/logs/rtk-hook.log")
RTK = next((p for p in ("/opt/homebrew/bin/rtk", "/usr/local/bin/rtk", os.path.expanduser("~/bin/rtk")) if os.path.exists(p)), "rtk")

_ARG = r"[\w@./~=:+,-]+"
_GIT = rf"git (?:status|log|branch)(?: (?!--(?:format|pretty|oneline|stat|patch|grep)|-p\b|--?[a-z]*[pP]\b){_ARG})*"
_LS = rf"ls(?: (?!-){_ARG})*"
_CD = rf"(?:cd {_ARG} && )?"
_PIPE = r"(?: \| (?:head|tail)(?: -n? ?\d+| -\d+)?| \| wc -l)?"
ALLOW = re.compile(rf"^{_CD}(?:{_GIT}|{_LS}){_PIPE}$")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except Exception:
        pass


def extract_command(payload):
    name = payload.get("toolName") or payload.get("tool_name") or ""
    if name.lower() not in ("bash", "shell"):
        return None
    args = payload.get("toolArgs", payload.get("tool_input", payload.get("toolInput")))
    if isinstance(args, str):
        args = json.loads(args)
    cmd = (args or {}).get("command")
    return cmd if isinstance(cmd, str) else None


def decide(raw, run=None):
    """Return the hook's stdout string ('' = no change)."""
    if os.environ.get("RTK_HOOK_OFF") == "1" or os.path.exists(OFF_FILE):
        return ""
    cmd = extract_command(json.loads(raw))
    if not cmd or "\n" in cmd or "<<" in cmd or not ALLOW.match(cmd.strip()):
        return ""
    r = (run or subprocess.run)([RTK, "hook", "copilot"], input=raw, capture_output=True, text=True, timeout=3)
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    out = json.loads(r.stdout)
    new = (out.get("modifiedArgs") or {}).get("command")
    if not isinstance(new, str) or "rtk " not in new or new == cmd:
        return ""
    log(f"rewrite: {cmd!r} -> {new!r}")
    return json.dumps(out)


def main():
    try:
        out = decide(sys.stdin.read())
        if out:
            sys.stdout.write(out)
    except Exception as e:
        log(f"error (fail-open): {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
