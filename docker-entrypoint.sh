#!/bin/sh
# Container entrypoint: fail fast without a Web UI password.
# Accepted: WEB_UI_PASSWORD, WEB_UI_PASSWORD_FILE, or a Docker/Compose secret mounted at
# /run/secrets/web_ui_password. Secret files are copied to a 0600 file first because the agent
# rejects group/world-readable secret files and Docker mounts secrets as 0444.
set -eu

if [ -z "${WEB_UI_PASSWORD:-}" ] && [ -z "${WEB_UI_PASSWORD_FILE:-}" ] && [ -r /run/secrets/web_ui_password ]; then
  WEB_UI_PASSWORD_FILE=/run/secrets/web_ui_password
fi

if [ -n "${WEB_UI_PASSWORD_FILE:-}" ] && [ -z "${WEB_UI_PASSWORD:-}" ]; then
  umask 077
  copy="$(mktemp /tmp/web_ui_password.XXXXXX)"
  cat "$WEB_UI_PASSWORD_FILE" > "$copy"
  export WEB_UI_PASSWORD_FILE="$copy"
fi

if [ -z "${WEB_UI_PASSWORD:-}" ] && [ ! -s "${WEB_UI_PASSWORD_FILE:-/nonexistent}" ]; then
  echo "ERROR: set WEB_UI_PASSWORD (or WEB_UI_PASSWORD_FILE / a web_ui_password secret). The Web UI is bound to 0.0.0.0 inside the container and must not run without a password." >&2
  exit 1
fi

exec "$@"
