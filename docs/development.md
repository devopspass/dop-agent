# Development notes

### Running tests

Unit tests (`dop-agent/tests/`) cover pure/isolated logic — parsing,
formatting, persistence against a throwaway sqlite file — with no live
Slack/Copilot dependency, so they run fast and fully offline:

```bash
python3 -m pip install pytest pytest-asyncio
cd dop-agent
python3 -m pytest -q
```

Run this (alongside `py_compile`) before every restart when refactoring.

Web UI browser tests (`dop-agent/ui_tests/`) are separate from the unit
suite above — they launch a real Chromium browser via Playwright against a
real (throwaway-DB) `web_server.app` instance, so they're slower and cover
actual DOM/JS behavior (draft persistence, session restore, etc.) that pure
Python unit tests can't:

```bash
python3 -m pip install playwright pytest-playwright
python3 -m playwright install chromium   # one-time browser download
cd dop-agent
python3 -m pytest ui_tests -q
```

For quick manual/exploratory checks against the live running bot's Web UI
(not the automated regression suite above), drive MS Edge via CDP instead —
see the `dop-agent-improvement` skill.


## Copilot SDK Key Details

- **Package**: `pip install github-copilot-sdk` → module name is `copilot` (not `github_copilot_sdk`)
- **Auth**: `COPILOT_GITHUB_TOKEN` env var (GitHub PAT) — passed directly to `CopilotClient(github_token=...)`
- **Client init**: `CopilotClient(working_directory=..., github_token=...)` — note `working_directory`, not `cwd`
- **Session**: `await client.create_session(on_permission_request=PermissionHandler.approve_all)`
- **Send**: `await session.send_and_wait("prompt", agent_mode="autopilot", timeout=120.0)`
- **Response**: `event.data.content` where `event.data` is `AssistantMessageData`

### Driver icons

`icon:` in `driver.yaml` (and `card.icon` per kind) accepts three forms; the registry resolves them into a ready-to-use `icon_url` (in `GET /api/integrations/ui` and `GET /api/drivers`) that `furls.js`/`settings.js` use as-is, hiding the image if it fails to load:

| Form | Example | Resolved `icon_url` |
|---|---|---|
| bare name | `icon: jira` | `/static/icons/jira.svg` (`webui/icons/`) |
| driver-folder file | `icon: logo.svg` (file in `drivers/<id>/`, also `~/.dop-agent/drivers/<id>/`) | `/api/drivers/<id>/icon/logo.svg` |
| URL | `https://cdn.example.com/x.png` or `data:image/(png\|svg+xml\|jpeg\|webp);base64,...` | unchanged |

- If `icon` is unset and `drivers/<id>/icon.svg` exists it is used automatically (`/api/drivers/<id>/icon`).
- The endpoint needs the web-UI login, serves only manifest-declared `*.svg` files (+ `icon.svg`) up to 64 KB, no symlinks, as `image/svg+xml` with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` and `X-Content-Type-Options: nosniff` (SVG can carry scripts).
- Manifests with other schemes (`javascript:`, `http:`, `file:`, ...) or oversized/missing files are rejected.
- The web UI sets no Content-Security-Policy, so `https:`/`data:` icons work without changes. Privacy trade-off: an `https://` icon makes the browser contact that third-party host; prefer a driver-folder file.
