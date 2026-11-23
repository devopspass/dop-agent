"""`${{ path.to.value }}` templates for package drivers (skills, watcher env, CLI args).

    render("run ${{config.account}}", {"config": {"account": "me@x.com"}})  -> "run me@x.com"
    render("literal $${{config.x}}", ctx)                                    -> "literal ${{config.x}}"

Only dotted lookups in the given context are supported (no expressions). An unknown or empty path raises
`TemplateError` so a typo never silently renders as an empty string. Contexts hold option values and
paths only - never secret values (secrets are referenced by env-var NAME, e.g. `${{config.token_env}}`).
"""
from __future__ import annotations

import re
from typing import Any

from .base import DriverError

_TOKEN_RE = re.compile(r"(\$)?\$\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


class TemplateError(DriverError):
    """Template references an unknown or empty value."""


def fields(text: str) -> set[str]:
    """Dotted paths referenced (escaped `$${{...}}` ones excluded)."""
    return {m.group(2) for m in _TOKEN_RE.finditer(text) if not m.group(1)}


def lookup(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise TemplateError(f"unknown template value ${{{{{path}}}}}")
        cur = cur[part]
    if cur is None or cur == "":
        raise TemplateError(f"template value ${{{{{path}}}}} is empty - set it in the integration options")
    return cur


def render(text: str, ctx: dict) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1):  # `$${{x}}` -> literal `${{x}}`
            return m.group(0)[1:]
        return str(lookup(ctx, m.group(2)))
    return _TOKEN_RE.sub(sub, text)
