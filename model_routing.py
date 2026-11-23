"""
Per-session model routing for child/research sessions.

The Copilot SDK accepts ``model=`` on both ``create_session`` and
``resume_session``; without it a session runs on the CLI default from
``~/.copilot/settings.json``.

This module is pure policy + small caches, no Slack/web imports:

* ``resolve_model(...)`` -> ``Routing`` (chosen model, reason, visible note).
  Explicit ``model`` wins over ``task_kind``; unknown/unavailable models fall
  back to the default with a note; sessions that look like prod changes
  (CHG, prod/prd, terraform apply, deploy ...) are NEVER put on a model cheaper
  than the default (fail closed).
* ``MODEL_ROUTING`` mode: ``on`` (default) | ``dry`` (log what would be
  routed, change nothing) | ``off`` (kill switch: no ``model=`` is ever passed).
* Pricing in AIU per Mtok. The SDK's ``client.list_models()`` exposes real
  ``billing.token_prices`` per model, so live rates are preferred; the static
  table is the fallback.
* ``release-loop`` kind: delegation of a long multi-step change loop to a fresh
  small-context session. It stays on the DEFAULT model (merge/cinc/terraform
  apply are prod-affecting); the saving comes from the small context. It
  requires a handoff brief.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import config
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MODES = ("on", "dry", "off")

# task_kind -> "cheap" | "default". Cheap kinds are read/summarise work that
# does not change anything; everything that writes code or touches prod stays
# on the default model.
CHEAP_KINDS = frozenset({"research", "summary", "log-triage", "status-report", "poll-review"})
DEFAULT_KINDS = frozenset({"code", "design", "review", "prod-ops", "terraform", "change", "release-loop"})
TASK_KINDS = CHEAP_KINDS | DEFAULT_KINDS

FALLBACK_DEFAULT_MODEL = "claude-sonnet-5.5"
DEFAULT_CHEAP_MODEL = "claude-haiku-5.5"

# Static fallback allow-list used when the SDK model list is unavailable.
STATIC_AVAILABLE = frozenset({
    "auto", "claude-sonnet-5", "claude-sonnet-5.5", "claude-haiku-5.5", "claude-opus-5", "claude-opus-5.5",
    "gpt-5.4", "gpt-5.3-codex", "gpt-5.4-mini", "gpt-5-mini", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol", "gemini-3.7-flash", "gemini-3.8-flash", "grok-4.5", "grok-4.6",
    "grok-4.7", "kimi-k3", "mai-code-1.1-flash",
})

# AIU per Mtok: (input, cache_read, cache_write, output). "fitted" = least
# squares over session.shutdown token details; "sdk" = list_models billing.
STATIC_RATES: dict[str, dict] = {
    "claude-sonnet-5": {"rates": (200, 20, 250, 1000), "source": "fitted (exact, 957 sessions)"},
    "claude-sonnet-5.5": {"rates": (200, 10, 250, 1000), "source": "sdk list_models"},
    "claude-haiku-5.5": {"rates": (10, 1, 12, 50), "source": "sdk list_models (<=100k ctx)"},
    "claude-haiku-4.5": {"rates": (100, 10, 125, 500), "source": "fitted (estimated)"},
    "claude-opus-4.8": {"rates": (500, 50, 625, 2500), "source": "fitted (exact, 34 sessions)"},
    "gpt-5.4": {"rates": (207, 29, 0, 1186), "source": "fitted"},
    "gpt-5.3-codex": {"rates": (157, 16, 0, 1260), "source": "fitted"},
    "gpt-5.4-mini": {"rates": (67, 7, 0, 405), "source": "fitted"},
}

_PROD_RE = re.compile(
    r"\b(CHG-?\d*|prod(?:uction)?|prd|mig-prd|terraform\s+apply|terragrunt\s+apply|run-all\s+apply"
    r"|deploy(?:ment|ing)?|change[- ]request|non-regular change|approve|merge\s+(?:the|this|it|my|mr|!\d+|into))\b",
    re.IGNORECASE,
)

RELEASE_LOOP_BRIEF_TEMPLATE = (
    "Handoff brief (required for task_kind='release-loop'):\n"
    "1. Goal: what is being released (repo/module + version, ticket key).\n"
    "2. Links: all relevant URLs / branches (full https:// links).\n"
    "3. Steps to run: the ordered list of steps to execute.\n"
    "4. Definition of done: explicit end state to report back.\n"
    "5. Stop-and-ask conditions (prod-affecting step, a failure you cannot fix)."
)


@dataclass
class Routing:
    model: str                 # effective model name (default included), for display/estimates
    pass_model: str | None     # value to hand to the SDK; None = keep the CLI default
    reason: str
    note: str | None = None    # visible note (fallbacks, guard, dry-run)
    task_kind: str | None = None
    mode: str = "on"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"model": self.model, "pass_model": self.pass_model, "reason": self.reason,
                "note": self.note, "task_kind": self.task_kind, "mode": self.mode}


def routing_mode() -> str:
    mode = config.value("agent.model_routing.mode").lower()
    return mode if mode in MODES else "on"


def default_model() -> str:
    env = config.value("agent.model_routing.default_model")
    if env:
        return env
    try:
        data = json.loads((Path.home() / ".copilot" / "settings.json").read_text())
        if isinstance(data.get("model"), str) and data["model"]:
            return data["model"]
    except Exception:
        pass
    return FALLBACK_DEFAULT_MODEL


def cheap_model() -> str:
    return config.value("agent.model_routing.cheap_model") or DEFAULT_CHEAP_MODEL


# --- pricing -----------------------------------------------------------------

_live_rates: dict[str, tuple] = {}


def rates_for(model: str) -> tuple[tuple[float, float, float, float], str] | None:
    if model in _live_rates:
        return _live_rates[model], "sdk list_models"
    entry = STATIC_RATES.get(model)
    return (entry["rates"], entry["source"]) if entry else None


def estimate_aiu(model: str, *, input_tokens: int = 0, cache_read: int = 0, cache_write: int = 0,
                 output_tokens: int = 0) -> dict:
    """Estimated AIU for a token mix. Always carries the rate source so
    callers can say 'estimated' vs 'exact'."""
    found = rates_for(model)
    if not found:
        return {"model": model, "aiu": None, "source": "unknown model"}
    (r_in, r_cr, r_cw, r_out), source = found
    aiu = (input_tokens * r_in + cache_read * r_cr + cache_write * r_cw + output_tokens * r_out) / 1e6
    return {"model": model, "aiu": round(aiu, 4), "source": source}


def price_index(model: str) -> float | None:
    """Relative cost for 'is X pricier than Y' decisions (output price)."""
    found = rates_for(model)
    return found[0][3] if found else None


def parse_model_info_rates(info) -> tuple | None:
    try:
        p = info.billing.token_prices
        return (p.input_price, p.cache_read_price, p.cache_write_price, p.output_price)
    except Exception:
        return None


# --- available models (allow-list) -------------------------------------------

_AVAIL_TTL = 3600.0
_avail_cache: tuple[float, frozenset[str]] | None = None


def _remember(infos) -> frozenset[str]:
    ids = set()
    for m in infos:
        ids.add(m.id)
        r = parse_model_info_rates(m)
        if r:
            _live_rates[m.id] = tuple(r)
    return frozenset(ids)


async def available_models(client=None, *, timeout: float = 25.0) -> frozenset[str]:
    """Allow-list from the SDK (cached 1 h); static fallback if the call fails."""
    global _avail_cache
    now = time.monotonic()
    if _avail_cache and now - _avail_cache[0] < _AVAIL_TTL:
        return _avail_cache[1]
    try:
        if client is not None:
            ids = _remember(await asyncio.wait_for(client.list_models(), timeout))
        else:
            from copilot import CopilotClient  # lazy: tests and pure policy don't need the SDK
            c = CopilotClient(github_token=config.copilot_github_token() or None)
            await c.__aenter__()
            try:
                ids = _remember(await asyncio.wait_for(c.list_models(), timeout))
            finally:
                await c.__aexit__(None, None, None)
        if ids:
            _avail_cache = (now, ids)
            return ids
    except Exception as exc:
        log.warning("model_routing: list_models failed (%s) - using static allow-list", exc)
    return STATIC_AVAILABLE


def _reset_caches_for_tests() -> None:
    global _avail_cache
    _avail_cache = None
    _live_rates.clear()


# --- policy --------------------------------------------------------------------

def looks_prod_affecting(*texts: str | None) -> bool:
    return any(t and _PROD_RE.search(t) for t in texts)


def validate_release_brief(prompt: str) -> None:
    """release-loop delegations need a real handoff, otherwise the fresh
    session guesses and reruns eat the saving."""
    p = prompt or ""
    if len(p) < 200 or "http" not in p or not re.search(r"\b(done|finish|complete)\b", p, re.IGNORECASE):
        raise ValueError(
            "task_kind='release-loop' needs a handoff brief in the prompt (>=200 chars, at least one full "
            "https:// link and an explicit definition of done). Template:\n"
            + RELEASE_LOOP_BRIEF_TEMPLATE
        )


def resolve_model(
    model: str | None = None,
    task_kind: str | None = None,
    *,
    prompt: str = "",
    title: str = "",
    parent_title: str = "",
    parent_model: str | None = None,
    available: frozenset[str] | set[str] | None = None,
    mode: str | None = None,
    default: str | None = None,
) -> Routing:
    mode = mode or routing_mode()
    default = default or default_model()
    kind = (task_kind or "").strip().lower() or None
    model = (model or "").strip() or None

    if kind is not None and kind not in TASK_KINDS:
        raise ValueError(
            f"unknown task_kind {task_kind!r} - valid: {', '.join(sorted(TASK_KINDS))} "
            "(omit it to keep the default model)"
        )
    if kind == "release-loop":
        validate_release_brief(prompt)

    if mode == "off":
        note = "MODEL_ROUTING=off: model/task_kind ignored, default model used" if (model or kind) else None
        return Routing(default, None, "routing disabled (MODEL_ROUTING=off)", note, kind, mode)

    notes: list[str] = []
    if model:
        candidate, reason = model, f"explicit model={model}"
    elif kind in CHEAP_KINDS:
        candidate, reason = cheap_model(), f"task_kind={kind} -> cheap model"
    elif kind:
        candidate, reason = default, f"task_kind={kind} -> default model"
    else:
        candidate, reason = default, "no task_kind/model -> default model"

    allowed = available if available else STATIC_AVAILABLE
    if candidate != default and candidate not in allowed:
        notes.append(f"model {candidate!r} is not available (allowed: {', '.join(sorted(allowed))}) - fell back to {default}")
        candidate, reason = default, reason + " (unavailable -> default)"

    if candidate != default:
        cand_p, def_p = price_index(candidate), price_index(default)
        cheaper = cand_p is not None and def_p is not None and cand_p < def_p
        # Unknown price of a non-default model: treat as possibly cheaper (fail closed).
        if (cand_p is None or cheaper) and looks_prod_affecting(prompt, title, parent_title):
            notes.append(f"prod/CHG/terraform-apply/deploy wording detected - guard kept {default} instead of {candidate}")
            candidate, reason = default, reason + " (prod guard -> default)"
        else:
            par_p = price_index(parent_model) if parent_model else None
            if not model and par_p is not None and cand_p is not None and cand_p > par_p:
                notes.append(f"{candidate} is pricier than the parent's {parent_model}")

    pass_model = candidate if candidate != default else None
    if mode == "dry" and pass_model is not None:
        notes.append(f"dry-run: would route to {candidate}; running on {default}")
        return Routing(default, None, "dry-run: " + reason, "; ".join(notes), kind, mode)
    return Routing(candidate, pass_model, reason, "; ".join(notes) or None, kind, mode)
