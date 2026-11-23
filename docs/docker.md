# Running DOP Agent in Docker

## Quick start

```bash
cd dop-agent
export WEB_UI_PASSWORD='choose-a-strong-password'   # mandatory; compose refuses to start without it
export COPILOT_GITHUB_TOKEN='<token with Copilot access>'   # optional here; see "Copilot authentication"
docker compose up -d --build
open http://localhost:10818        # log in with WEB_UI_PASSWORD
```

State (config, SQLite stores, audio) lives in the `dop-agent-data` volume (`/home/dop/.dop-agent`), the Copilot
runtime state in `dop-agent-copilot` (`/home/dop/.copilot`). Put a `config.yaml` into the data volume to configure
integrations (see `config.example.yaml`).

## Build options

| Build arg | Default | Effect |
|---|---|---|
| `INSTALL_RENDER` | `0` | Installs `requirements-render.txt` (WeasyPrint, Pillow, PyMuPDF) plus cairo/pango/gdk-pixbuf/fonts. Needed for table/Markdown → PNG/PDF rendering. PyMuPDF is AGPL-licensed: enabling this extra means you accept its licence terms. |
| `INSTALL_VOICE` | `0` | Installs `requirements-voice.txt` (openai-whisper, pulls PyTorch, large image) plus `ffmpeg`. |
| `PREFETCH_COPILOT_CLI` | `1` | Downloads the Copilot runtime at build time (needs network). `0` = download on first run. |

With compose: `INSTALL_RENDER=1 docker compose up -d --build`.

## Copilot CLI / runtime

`github-copilot-sdk` is a pure-Python wheel. It does **not** bundle the Copilot CLI and does **not** need Node.js or
`npm i -g @github/copilot`: on first use it downloads a checksum-verified runtime bundle from GitHub Releases into
`~/.cache/github-copilot-sdk` (the image pre-fetches it with `python -m copilot download-runtime`). For air-gapped
hosts, provision the binary yourself and set `COPILOT_CLI_PATH` (and `COPILOT_SKIP_CLI_DOWNLOAD=1`).

## Copilot authentication

Pass a token via `COPILOT_GITHUB_TOKEN` (or `GITHUB_TOKEN`), or log in once inside the container and keep the
`dop-agent-copilot` volume.

## Web UI exposure

- Inside the container the server binds `WEB_UI_HOST=0.0.0.0` (required for port publishing).
- `docker-compose.yml` publishes only `127.0.0.1:10818`. Do not change that to `0.0.0.0`/`10818:10818` unless TLS
  and an authenticating reverse proxy sit in front: the UI controls an agent that can run tools.
- The Web UI rejects requests whose `Host` header is not allow-listed (DNS-rebinding guard). The default allow-list is
  `127.0.0.1`, `localhost` and `[::1]` on port `10818`. If you reach the UI through another hostname or host port,
  add `host:port` entries to `WEB_UI_ALLOWED_HOSTS` (comma separated), e.g. `WEB_UI_ALLOWED_HOSTS=agent.example.org:443`.
- `WEB_UI_PASSWORD` is mandatory; alternatives are `WEB_UI_PASSWORD_FILE=/path` or a Compose secret named
  `web_ui_password` mounted at `/run/secrets/web_ui_password` (the entrypoint copies it to a `0600` file because the
  agent rejects group/world-readable secret files).
- `HEALTHCHECK` requests the unauthenticated `/login` page on `127.0.0.1:$WEB_UI_PORT` inside the container.

## systemd (Linux, without Docker)

`./bot_ctl.sh install && ./bot_ctl.sh start` installs and starts a `systemctl --user` unit
(`~/.config/systemd/user/dop-agent.service`). Use `DOP_AGENT_PYTHON=/path/to/python3.12+` to choose the interpreter,
`./bot_ctl.sh logs` to follow the journal, and `loginctl enable-linger $USER` to keep it running after logout.
