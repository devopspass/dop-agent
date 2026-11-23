---
name: dop-agent-driver-development
description: "Use when: creating or converting a dop-agent integration/driver (package driver like ms_teams: driver.yaml, cli/, watch/, skill template, furls, Test button, watcher recipes, migration of old skills/watches). Covers the flow from the user questionary to implementation, tests and rollout. Load together with dop-agent-improvement."
argument-hint: "[service/driver to build or convert, e.g. ms_outlook]"
---

# DOP Agent — New Driver / Integration Development

> **MANDATORY GATE: ask the Phase 2 questionary (via `ask-format`) and receive the user's answers BEFORE any implementation.**
> Until answered: no code, edits, commits, todos-as-work or watches (discovery is read-only). Run a follow-up round if answers are unclear.
> Autopilot / "decide yourself" / "skip Phase 2" in a task prompt does NOT waive it unless the user explicitly says so in the current conversation. Skipping it caused rework (missing watchers/CLI areas, wrong auth, leftover files).

Reference implementation: `dop-agent/drivers/ms_teams/` (commits a283c40, 7093126, 5e5650d, 77f84eb) and `ms_outlook`.
Always also load `dop-agent-improvement` (commit-then-restart discipline) and read the module docstrings of
`drivers/base.py`, `drivers/package.py`, `drivers/manifest.py` (`_parse_package`), `drivers/templating.py`,
`integrations/skills.py`, `integrations/registry.py`, `watch_scripts.py` (`apply_watcher`).

## Phase 0 — Driver type
| Type | Use when |
|---|---|
| `package` (default for new work) | Service reached through its own CLI; agent uses the CLI from a per-instance skill; watchers are `script` watches; no polling in the bot |
| Python `Driver` subclass (`drivers/<id>/__init__.py`, like `jira`, `gitlab`) | Simple HTTP API; bot polls via `fetch`/`poll`/`test_connection` |

The rest of this skill describes the package type.

## Phase 1 — Discovery (read-only)
Discussion only: no edits, commits or watches until the user says proceed.
1. Read the reference driver and any existing CLI/skill/watcher for the service (e.g. `drivers/ms_outlook/cli/`, `scripts/watch/<svc>_watch.py`, `skills/<svc>/`, `tests/test_<svc>_watch.py`).
2. Check the real config shape in `~/.dop-agent/config.yaml` (never print secrets).
3. Open the user's existing instances (e.g. Teams `main`) to mirror their options.

## Phase 2 — Questionary (`ask-format` skill) — MUST be asked and answered before implementation
Post a <150-word plan summary (with your discovery findings and recommendation per question), then ask ONE `ask` block (≤14 questions grouped by topic, recommended `*`, each with a free-text escape; drop items already decided in the prompt or by an earlier round).
| # | Topic | Ask / typical options (default `*`) |
|---|---|---|
| **Access & secrets** | | |
| Q1 | Access method | official REST/API + token `*` (GitLab convention, `${env:VAR}`, only the instance name travels) / secretless browser session (CDP tab token: say what happens when the token expires, re-grab) / kubeconfig or cloud IAM. Who creates the token/instance: the user configures himself `*` |
| Q2 | Instances & options | single vs multiple `*` instances; per-instance options (`base_url`, account/tenant, default project/scope, `verify_tls`) with generic examples, no built-in host; token env var name per instance |
| **Scope & safety** | | |
| Q3 | v1 scope | list ALL feature areas of the service (e.g. core objects AND CI/Actions/pipelines, alerts, queues) and mark each in/out; read-only / read + confirm-first writes `*` / full |
| Q4 | Write safety | draft-first, confirm target (account, recipients, project, env) before send/delete/create/promote; operations never exposed (destructive, secrets) |
| Q5 | Existing assets | port from an old skill/MCP/CLI/script or git history (`git log --diff-filter=D`) vs fresh; separate integration `*` vs linked to a related one; old pieces to remove |
| **Watchers & furls** | | |
| Q6 | Watcher recipes | which events (one per area from Q3), default scope and bounds, filter flags; "wait for a reply from X" = explicit user-given lists, no built-in VIP lists; first run = silent baseline `*`; auth-expiry behavior |
| Q7 | Furl kinds | which URL shapes to unfurl and card fields; tenant/host matching by option `*`, no match → plain link |
| **Skill** | | |
| Q8 | Skill naming/identity | `<name>-<instance>` `*`; identity (account/title) in the description; description generic, no CLI hints; all CLI subcommands and watcher flags documented in the skill body only (no extra `WATCHERS.md`/README per driver) |
| **Testing** | | |
| Q9 | Verification | mocked backend for unit/CLI/watcher dry-runs `*` (no real credentials); Test-button steps (config → auth → whoami; "ok" = reachable AND authenticated); what the user will test live; sample links/ids for furls |
| **Rollout** | | |
| Q10 | Migration & cleanliness | remap old watches by account, delete old skill/MCP refs/CLI folders (no symlinks/shims); driver folder self-contained; dop-agent core stays service-agnostic (no service polling/refs outside the driver) |
| Q11 | Runtime & deps | bot interpreter only, scripts always Python; packages to install into it (reported by Test); who restarts the daemon (user `*`, or agent at the end) |
| Q12 | Process | parallel sessions in the tree? commit only own paths (`git add <paths>`), no stash/reset/checkout/clean; end with a self-review watch `*` |

Finish with a decision table (Driver, Access, Instances, Scope per area, Write safety, Watchers, Furls, Skill, Test steps, Migration, Rollout) and a quick-reply `actions` block: "Start implementation" / "Show step breakdown". Do not implement until the user chooses.

## Phase 3 — Implementation plan (todos)
Create SQL todos with real dependencies. Typical order:
1. `driver.yaml` + option patterns
2. Move CLI into `drivers/<id>/cli/` (`git mv`), add `fetch` and `test` subcommands
3. Watcher script(s) into `drivers/<id>/watch/`
4. Skill template `skill/SKILL.md.tmpl`
5. Registry/skills wiring, migration (move CLI code into the driver folder, leave no symlinks)
6. Tests
7. Docs (README, registry docstring; watcher docs in the skill)
8. Review, commit, restart, live verification

## Phase 4 — Build
### `driver.yaml` (`type: package`, off by default)
- `id`, `title`, `icon` (vendor logo vendored as small script-free `icon.svg` in the driver folder, e.g. from simple-icons; never hot-linked), `auth.types` (secretless auth such as `cdp_edge`: `fields: {}`).
- `options`: each with `type`, `default`, `required`, `help`, and a full-match `pattern:` for every string option (no newlines, quotes, backticks, `$`, `<>`; keeps skills and argv injection-safe).
- `tenant: { option: tenant_id }` when links must be matched to an instance.
- `cli: { entry, global_args }` with `${{config.x}}` templates; `env:` for watcher scripts.
- `skill: { template, name }` → generated skill `<name>-<instance>`.
- `watchers:` recipes `{ script, args, emoji, slack_emoji, title }`.
- `kinds:` with `url_match` (named groups), optional `url_template`, `card` (`fields`, `icon`, `label`).
- `version`.

### CLI (`cli/cli.py`)
- Runs under the bot's own interpreter (`sys.executable`, `${{driver.python}}`); needed packages (e.g. `websocket-client`) must be installed there.
- Required subcommands: `fetch --kind K --ident '<json>'` → one JSON `{title, status?, status_emoji?, url?, fields?}`; `test` → `{ok, message, steps?, set_options?}` (`set_options`, e.g. `tenant_id`, is persisted by the settings API).
- Test steps are explicit and individually reported (e.g. cdp, tab, token, graph_me) plus a missing-dependency report; 70 s limit.
- Bound every scan (limits, time windows); write ops are draft-first; never print tokens.
- Atomic file writes (tmp + rename) for caches/state; quote user input safely.
- Run `test` once for real: an undefined helper in the Teams CLI broke every call and Test.

### Token handling (reference: `drivers/gitlab/`) — use for every token/API-key driver
- `driver.yaml`: `auth.types.<type>.fields.token: { secret: true, required: true }` (`private_token` for GitLab; keep the same shape, other field names only when the service needs them, e.g. `client_id`/`client_secret`); `settings.base_url` required with a generic `example:`; no built-in host. `cli.global_args: ["--instance", "${{instance.name}}"]` and `env: <SVC>_INSTANCE: "${{instance.name}}"` — only the instance NAME travels, never the token.
- `config.yaml`: `integrations.<name>: {driver, enabled, base_url, auth: {type, token: '${env:VAR}'}}`; refs may be `${env:X}`, `${file:P}`, `${keychain:S}`. Never put a literal token in config, skills, argv, env of subprocesses, or logs.
- `cli/<svc>config.py` (copy `glconfig.py`): `load_instance(name=None) -> (name, base_url, token, options)`; reads `~/.dop-agent/config.yaml` (override `DOP_AGENT_CONFIG`), loads `dop-agent/.env` (`override=False`), resolves refs with `config.resolve_ref`; `name=None` → `<SVC>_INSTANCE` env or the single enabled instance of that driver (several → error listing them); a missing/disabled/renamed instance or an empty token raises `InstanceError` (never fall back to another instance). CLI, `test` and watch scripts all use this one function.
- Redact `(token|password|secret|authorization)[=:]\s*\S+` in every printed error; `test` reports steps `config` (instance → base_url), `auth` (HTTP status, "check auth.token scope/expiry"), never the token.

### Watcher scripts (`watch/<svc>_watch.py`)
- Exit codes: 0 silent, 200 changed (output wakes the model), other = error. State lives under `WATCH_STATE_DIR`.
- First run records a baseline (no flood of old items). Cheap, bounded scans; filters are flags.
- Read-only, `chmod 755`, not group/world-writable. Use `watch_script(script=..., watcher=<recipe>, integration=<instance>)`; plugin drivers in `~/.dop-agent/drivers/<id>/` must pass `dry_run` first.
- Env comes from `driver.yaml env:`; `instance_env` must fail for a missing/disabled/renamed instance (never run as another account).

### Skill template (`skill/SKILL.md.tmpl`)
- Frontmatter `name: ${{skill.name}}`, a short generic "Use when: ANY <service> read/action on <base_url> (`${{config.title}}`) — <3-5 topics>, and watchers." description. NEVER put the CLI command/path, `--instance`, "CLI:" or usage hints in the description: it is always visible, so the agent runs the CLI without loading the skill and guesses the syntax. Command path and usage live only in the body.
- Absolute paths via `${{driver.python}}`, `${{driver.cli}}`, `${{driver.watch_dir}}`; tell the agent not to explore script paths (run `--help` from the driver folder if needed).
- List every CLI subcommand with defaults and flags, every watcher recipe with flags and filter semantics, and how to bound scans.
- Write-safety rules (confirm account and recipients before send/reply/forward; confirm calendar writes).
- Say how to report dry-run versus registered watches clearly.
- Only `${{...}}` templating, no secrets. Renders are atomic into `~/.dop-agent/skills-gen/<skill>-<instance>/` before each session; a template error skips that instance and never raises.

### Wiring and migration
- Instance names mapping to the same skill suffix (`x_aws` vs `x-aws`) are rejected.
- Remove the old skill and the old CLI folder (CLI code lives only in `drivers/<id>/cli/`; no symlink shims), remap old watches to the new instance by account.
- Update together: `dop-agent/README.md` (driver paragraph), registry docstring, skills listing if relevant. Watcher docs live in the skill template; do not add a separate per-driver `WATCHERS.md`.

## Phase 5 — Testing
| Level | What |
|---|---|
| Unit (`dop-agent/tests`) | Manifest/option patterns (`test_package_driver.py`, `test_drivers_registry.py`), watcher script (`test_<svc>_watch.py`), icons (`test_driver_icons.py`), furls (`test_furls.py`), `watch_scripts`/`apply_watcher` |
| Skill render | Render the template for a fake instance; assert no leftover `${{`, correct absolute paths, bad options rejected |
| CLI | Run `test` and `fetch` against the real tab; run each watcher with a real dry run |
| Regression | `cd dop-agent && python -m pytest -q -x 2>&1 \| tail -30` |

Commit types: `feat|fix|refactor|chore|docs|ci` only; no `Co-authored-by` trailer.

## Phase 6 — Rollout and verification
1. Commit, then restart the daemon (atomic restart, see `dop-agent-improvement`).
2. In Settings enable the instance (the user may configure it himself), press Test, check per-step results and that `tenant_id` is saved.
3. Start a new session; verify skill `<name>-<instance>` is listed and renders correctly.
4. Register one watcher with `dry_run` first and show the output; then register for real and confirm with `watch_list`.
5. Paste a sample link and verify the furl card; with several instances confirm tenant matching (no match → plain link).
6. Verify old watches were remapped and the old skill is gone.
7. Report: what was built, test results, what the user must configure, feedback hooks.

## Gotchas (from Teams)
- Option validation, quoting, atomic renders.
- Bot Python lacks dependencies: report them in Test.
- Keep README, skill (watcher docs) and registry docstring in sync.
- Test must work for a not-yet-saved/renamed instance with a clear message (GitHub "instance not found/enabled"); a status "ok" in Settings means reachable + authenticated (auto-tested), not just configured.
- The agent must not run `--help` or hunt for CLI paths: the skill body documents every subcommand and watcher flag.
- Tokens: copy the GitLab convention exactly; never invent another format.
- No service-specific code in dop-agent core; no symlinks/leftover folders in the repo root.
- Watches pinned to a disabled instance must fail, not fall back.
