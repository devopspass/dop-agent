#!/usr/bin/env python3
"""Boot a throwaway DOP Agent Web UI populated with fully fictional demo data.

Nothing here touches the real bot_store.db, ~/.dop-agent or ~/.copilot: HOME and
DOP_AGENT_CONFIG are redirected to a temp dir BEFORE any project module is imported,
the message store / watchers DB / usage index live in that temp dir, and the
Copilot turn handler is a stub. Used by take_screenshots.py; also runnable by hand:

    python dop-agent/scripts/demo/seed_demo.py     # prints the URL + password, Ctrl-C to stop
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[2]
DEMO_PASSWORD = "demo-password"

# Isolate before importing project modules (config/usage_index read these at import time).
_TMP = Path(tempfile.mkdtemp(prefix="dop-demo-"))
(_TMP / "home").mkdir()
if sys.platform == "darwin":  # keep Playwright's browser cache findable after HOME moves
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/Library/Caches/ms-playwright"))
elif sys.platform != "win32":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/.cache/ms-playwright"))
os.environ["HOME"] = str(_TMP / "home")
os.environ["DOP_AGENT_CONFIG"] = str(_TMP / "config.yaml")
for _k in list(os.environ):
    if any(s in _k.upper() for s in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SLACK", "TELEGRAM", "GITLAB", "GITHUB", "JIRA")):
        del os.environ[_k]
sys.path.insert(0, str(BOT_DIR))

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import uvicorn  # noqa: E402

import bot_config  # noqa: E402
import message_store as ms  # noqa: E402
import usage_index  # noqa: E402
import watchers  # noqa: E402
import web_server  # noqa: E402

# --------------------------------------------------------------------------- content

CODE_MSG = """Here is the fix. The retry helper now backs off exponentially and gives up after five attempts:

```python
import random
import time


def with_retry(fn, attempts=5, base=0.5):
    for n in range(attempts):
        try:
            return fn()
        except ConnectionError:
            if n == attempts - 1:
                raise
            time.sleep(base * 2 ** n + random.random() / 10)
```

| Check | Result |
|---|---|
| Unit tests | 42 passed |
| Lint | clean |
| Pipeline | [!128 (fix: DEMO-123 retry with backoff)](https://git.example.com/demo/api/-/merge_requests/128) green |

Next step: merge and watch the staging deploy."""

MERMAID_MSG = """Proposed request flow for the new **checkout service**:

```mermaid
flowchart LR
    U[Browser] --> CDN[CDN]
    CDN --> ALB[Load balancer]
    ALB --> API[Checkout API]
    API --> DB[(Orders DB)]
    API --> Q[[Payment queue]]
    Q --> W[Payment worker]
    W --> PSP{{Payment provider}}
    W --> DB
```

The queue decouples the API from the payment provider, so a provider outage only delays confirmations."""

CHART_MSG = """Spend over the last sprint, split by workload:

```chart
type: donut
title: AIU spend by workload
center: 12.4k
cards: Total=12.4k, Interactive=7.9k, Watchers=4.5k
table: yes
data:
  - Interactive: 7900
  - Watchers: 4500
```

And the sprint burndown:

```chart
type: burndown
title: Sprint burndown
x: D1, D2, D3, D4, D5, D6, D7, D8, D9, D10
ideal: 40 -> 0
data:
  - Remaining: 40, 38, 33, 30, 24, 21, 15, 9, 4, 0
```"""

ASK_MSG = """Before I roll out the new alerting setup I need a few answers.

```ask title="Alerting rollout" id=alerting
Q1 single: Which channel should receive production alerts?
- slack*: Slack #ops-alerts | fastest, already used by the team
- email: Email distribution list | slower, but works offline
- pager: Pager rota | only for critical alerts
D1 single: How should we roll it out?
- big: All services at once | quick, but noisy if thresholds are off
- canary*: One service first | one week of tuning before the rest
D2 multi: Which environments are in scope?
- dev: Development
- stg*: Staging
- prod*: Production
Q2 text: Anything the on-call team has asked for that we should include?
? e.g. quiet hours, escalation rules
```"""

IDEAS_MSG = """Here are ideas to make the nightly build faster. Triage them with ok / nok / skip:

```ideas title="8 ideas to speed up CI" id=ci-speed
- [1] Cache dependencies between runs :: restore `pip` and `npm` caches keyed by lockfile hash | effort=S | prio=High
- [2] Split the test suite in 4 shards :: run shards in parallel jobs, merge reports | effort=M | prio=High
- [3] Use a slimmer base image :: switch to a `-slim` image and drop unused build tools | effort=S | prio=Med
- [4] Skip docs-only pipelines :: path filters so doc changes don't trigger the full build | effort=S | prio=Med
- [5] Remote build cache :: share compiled artifacts across branches | effort=L | prio=Med
- [6] Flaky-test quarantine :: auto-retry known flaky tests once and report them separately | effort=M | prio=Low
- [7] Nightly full run, fast PR runs :: only run the slow integration suite on a schedule | effort=M | prio=Low
- [8] Migrate runners to ARM :: cheaper and often faster for our workload | effort=L | prio=Low
```"""

ACTIONS_MSG = """The staging deploy finished and smoke tests are green.

```actions
- * Promote to production | promote DEMO-123 to production
- Roll back | roll back the staging deploy
```"""

# (title, group, pinned, hours_ago, needs_attention, [(role, text)...])
SESSIONS = [
    ("Fix flaky retry in API client", "Demo sprint", True, 1.0, False, [
        ("user", "DEMO-123: the API client sometimes fails on connection resets. Can you add retries with backoff?"),
        ("assistant", CODE_MSG),
    ]),
    ("Checkout service architecture", "Demo sprint", True, 2.5, False, [
        ("user", "Draw the request flow for the new checkout service as a diagram."),
        ("assistant", MERMAID_MSG),
    ]),
    ("Sprint 14 spend and burndown", "Demo sprint", False, 5.0, False, [
        ("user", "How much did we spend last sprint and are we on track?"),
        ("assistant", CHART_MSG),
    ]),
    ("Alerting rollout decisions", "Demo sprint", False, 0.4, True, [
        ("user", "Plan the rollout of the new alerting rules."),
        ("assistant", ASK_MSG),
    ]),
    ("Speed up the nightly build", "Demo sprint", False, 0.8, True, [
        ("user", "Give me ideas to speed up our CI pipeline."),
        ("assistant", IDEAS_MSG),
    ]),
    ("Promote DEMO-123 to production?", "Demo sprint", False, 0.2, True, [
        ("user", "Deploy the retry fix to staging and run the smoke tests."),
        ("assistant", ACTIONS_MSG),
    ]),
    ("Terraform: new VPC for the sandbox", "Infra experiments", False, 26.0, False, [
        ("user", "Create a sandbox VPC module with two private subnets."),
        ("assistant", "Done. The module is in `modules/sandbox-vpc` and the plan shows **6 to add, 0 to change, 0 to destroy**.\n\n```hcl\nmodule \"sandbox_vpc\" {\n  source   = \"./modules/sandbox-vpc\"\n  cidr     = \"10.20.0.0/16\"\n  az_count = 2\n}\n```"),
    ]),
    ("Docker image size review", "Infra experiments", False, 30.0, False, [
        ("user", "Why is the worker image 1.4 GB?"),
        ("assistant", "Three culprits:\n\n1. Build tools left in the final stage (~600 MB)\n2. A full `node_modules` copy that only the build needs (~450 MB)\n3. Test fixtures baked into the image (~120 MB)\n\nA multi-stage build brings it to roughly 300 MB."),
    ]),
    ("Backup restore drill notes", "Infra experiments", False, 50.0, False, [
        ("user", "Summarize yesterday's restore drill."),
        ("assistant", "Restore of the **orders** database took 14 min (target: 20 min). One gap: the runbook did not mention rotating the read-only credentials afterwards. I added a step."),
    ]),
    ("Release notes v2.3", None, False, 72.0, False, [
        ("user", "Draft release notes for v2.3 from the merged MRs."),
        ("assistant", "## v2.3\n\n- **Added** retry with backoff in the API client (DEMO-123)\n- **Fixed** timezone bug in the weekly report (DEMO-131)\n- **Changed** default page size to 50"),
    ]),
    ("Onboarding checklist for new hires", None, False, 120.0, False, [
        ("user", "Make an onboarding checklist for Sam Sample."),
        ("assistant", "- [x] Laptop and accounts\n- [x] Repo access\n- [ ] Intro call with Alex Example\n- [ ] First small ticket (DEMO-140)"),
    ]),
    ("Weekly report automation", None, False, 200.0, False, [
        ("user", "Can we automate the weekly status report?"),
        ("assistant", "Yes. A scheduled script can collect closed tickets and merged MRs and post a summary every Friday at 16:00. Want me to set it up?"),
    ]),
]

GROUPS = [
    ("Demo sprint", "#6c8cff", "🚀", "Work items of the current demo sprint"),
    ("Infra experiments", "#35b27a", "🧪", "Sandbox and infrastructure experiments"),
]


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- seeding


class _FakeSlack:
    async def auth_test(self, *a, **k):
        return {"ok": True}

    async def chat_postMessage(self, *a, **k):
        return {"ts": "1.1"}


async def _noop_handle(**kwargs):
    return None


def _seed_usage(sids: list[str]) -> None:
    conn = usage_index._connect()
    now = datetime.now()
    for i, sid in enumerate(sids):
        for d in range(30):
            for h in (10, 14, 16):
                aiu = 12 + ((i * 7 + d * 3 + h) % 29) * (3 if d < 10 else 1.2)
                hour = (now - timedelta(days=d)).strftime("%Y-%m-%d ") + f"{h:02d}"
                conn.execute("INSERT OR REPLACE INTO usage_hourly(sid, hour, aiu) VALUES (?,?,?)", (sid, hour, round(aiu, 1)))
        conn.execute(
            "INSERT OR REPLACE INTO usage_files(sid, tokens_json) VALUES (?, ?)",
            (sid, '{"input": 900000, "cache_read": 5200000, "cache_write": 310000, "output": 120000}'),
        )
    conn.commit()
    conn.close()
    usage_index._initial_scan_done = True
    usage_index.ensure_background_scan = lambda force=False: None
    usage_index._quota_cache.update(
        at=time.monotonic(), tried=0.0, error=None, fetched_at=time.time(),
        value={"unlimited": False, "used": 18240, "entitlement": 30000, "used_pct": 60.8, "remaining_pct": 39.2,
               "overage": 0, "reset_at": (datetime.now(timezone.utc) + timedelta(days=9, hours=3)).isoformat()},
    )


def _seed_watches(ids: dict[str, str]) -> None:
    c1, c2, c3 = ids["Fix flaky retry in API client"], ids["Checkout service architecture"], ids["Weekly report automation"]
    specs = [
        ("mr", c1, "demo/api#128", None),
        ("pipeline", c1, "demo/api@feature/DEMO-123-retry-backoff", None),
        ("time", c3, "cron:daily 16:00", "Post the weekly status report summary"),
        ("time", c2, "in:2h", "Check whether the design review comments arrived"),
    ]
    for typ, cid, key, label in specs:
        try:
            watchers.add_watch(typ, cid, key=key, label=label, announce=False)
        except Exception as exc:  # type not enabled in this build: skip, keep demo usable
            print(f"[demo] watch {typ} {key} skipped: {exc}", file=sys.stderr)


def seed() -> dict[str, str]:
    """Fill the (already redirected) stores. Returns {title: conversation_id}."""
    groups = {t: ms.create_group(t, color=c, emoji=e, description=d)["id"] for t, c, e, d in GROUPS}
    ids: dict[str, str] = {}
    sids: list[str] = []
    for title, group, pinned, hours_ago, attention, msgs in SESSIONS:
        sid = f"demo-copilot-{len(sids):02d}"
        sids.append(sid)
        cid = ms.get_or_create_conversation(None, copilot_session_id=sid)
        ids[title] = cid
        t = hours_ago + 0.05 * len(msgs)
        for role, text in msgs:
            ms.record_message(cid, role, text, created_at=_iso(t), updated_at=_iso(t))
            t -= 0.05
        ms.set_title(cid, title)
        if group:
            ms.assign_session_group(cid, groups[group])
        if pinned:
            ms.set_pinned(cid, True)
        if not attention:  # sessions awaiting the user's answer stay unread
            ms.mark_read(cid)
    _seed_watches(ids)
    _seed_usage(sids)
    return ids


# --------------------------------------------------------------------------- server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start():
    """Seed and start the server. Returns (base_url, {title: conversation_id}, stop_fn)."""
    ms.DB_PATH = _TMP / "bot_store.db"
    ms._TITLES_FILE = _TMP / "session_titles.json"
    usage_index.DB_PATH = _TMP / "usage_index_v2.db"
    usage_index.SESSION_STATE_DIR = _TMP / "no-session-state"
    watchers.DB_PATH = _TMP / "watchers.db"
    watchers.init_db()
    watchers.check_watch_resource_exists = lambda *a, **k: None
    ms.init_db()

    web_server.WEB_UI_PASSWORD = DEMO_PASSWORD
    web_server._initialized = False
    web_server._START_TIME = time.time() - (3 * 86400 + 4 * 3600)
    web_server._BOT_VERSION = "0.1.0"
    # Static, clearly fake status-strip data instead of probing this machine.
    bot_config.COPILOT_WORKDIR = "/home/demo/workspace"
    bot_config.MCP_SERVERS = {"issues": {}, "gitlab": {}, "docs": {}}
    bot_config.probe_mcp_server_statuses = lambda: [
        {"name": n, "status": "ok", "detail": "configured"} for n in ("issues", "gitlab", "docs")
    ]
    ids = seed()
    web_server.init(slack_client=_FakeSlack(), handle_fn=_noop_handle, convos={}, is_authorized=lambda u: True, allowed_user_id="U_DEMO")

    port = _free_port()
    web_server.ALLOWED_HOSTS = frozenset(web_server.ALLOWED_HOSTS | {f"127.0.0.1:{port}"})
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("demo server did not start")

    def stop():
        server.should_exit = True
        thread.join(timeout=5)

    return f"http://127.0.0.1:{port}", ids, stop


if __name__ == "__main__":
    url, _ids, stop = start()
    print(f"Demo Web UI: {url}  (password: {DEMO_PASSWORD})  data dir: {_TMP}\nCtrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop()
