#!/usr/bin/env bash
# bot_ctl.sh — install and control dop-agent as a per-user service:
# a launchd agent on macOS, a `systemctl --user` unit on Linux.
#
# Usage: bot_ctl.sh {install|start|stop|restart|status|logs}
#   install  render the plist (macOS) / unit (Linux) template for this checkout
#
# Python interpreter: $DOP_AGENT_PYTHON, else the first python3.13 / python3.12 / python3 (>= 3.12) found.

set -euo pipefail

LABEL="com.dop-agent"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
OS_NAME="$(uname -s)"

resolve_python() {
  local candidate resolved
  if [[ -n "${DOP_AGENT_PYTHON:-}" ]]; then
    printf '%s\n' "$DOP_AGENT_PYTHON"
    return 0
  fi
  for candidate in python3.13 python3.12 python3; do
    resolved="$(PATH="$PATH:/opt/homebrew/bin:/usr/local/bin" command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$resolved" ]] && "$resolved" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  printf '%s\n' "python3"
}
PYTHON="$(resolve_python)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
ERROR_LOG="$HOME/.copilot/dop-agent-error.log"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="${DOMAIN}/${LABEL}"
TIMEOUT_SECS=20

usage() {
  echo "Usage: $0 {install|start|stop|restart|status|logs}" >&2
  exit 1
}

is_loaded() {
  launchctl print "$SERVICE_TARGET" >/dev/null 2>&1
}

pid_of() {
  launchctl list | awk -v label="$LABEL" '$3 == label {print $1}'
}

status() {
  local pid
  if is_loaded; then
    pid="$(pid_of || true)"
    if [[ -n "${pid:-}" && "$pid" != "-" ]]; then
      echo "✅ running (pid $pid)"
    else
      echo "⚠️ loaded but not running"
    fi
  else
    echo "❌ not loaded"
  fi
  if [[ -f "$ERROR_LOG" ]]; then
    echo "--- last 10 lines of $ERROR_LOG ---"
    tail -n 10 "$ERROR_LOG"
  fi
}

wait_for_pid() {
  local waited=0
  while (( waited < TIMEOUT_SECS )); do
    local pid
    pid="$(pid_of || true)"
    if [[ -n "${pid:-}" && "$pid" != "-" ]]; then
      echo "✅ ${LABEL} is up (pid $pid) after ${waited}s"
      return 0
    fi
    sleep 1
    ((waited += 1))
  done
  echo "❌ ${LABEL} did not come back up within ${TIMEOUT_SECS}s" >&2
  return 1
}

wait_for_stop() {
  local waited=0
  while (( waited < TIMEOUT_SECS )); do
    if ! is_loaded; then
      echo "✅ ${LABEL} stopped"
      return 0
    fi
    sleep 1
    ((waited += 1))
  done
  echo "❌ ${LABEL} is still loaded after ${TIMEOUT_SECS}s" >&2
  return 1
}

require_install() {
  if [[ ! -f "$PLIST" ]]; then
    echo "❌ installed plist not found: $PLIST" >&2
    echo "Run: $0 install" >&2
    return 1
  fi
  if ! plutil -lint "$PLIST" >/dev/null; then
    echo "❌ invalid launchd plist: $PLIST" >&2
    return 1
  fi
}

check_runtime() {
  local errors=0
  if [[ ! -x "$PYTHON" ]]; then
    echo "❌ required interpreter not found: $PYTHON (need Python 3.12+; set DOP_AGENT_PYTHON)" >&2
    errors=1
  elif ! "$PYTHON" - "$SCRIPT_DIR" <<'PY'
import importlib.util
import sys

sys.path.insert(0, sys.argv[1])
import config

modules = ["copilot", "dotenv", "yaml"]
if (config.secret_value("adapters.slack.bot_token")
        and config.secret_value("adapters.slack.app_token")):
    modules.extend(("slack_bolt", "slack_sdk", "aiohttp"))
missing = [name for name in modules if importlib.util.find_spec(name) is None]
if missing:
    print("Missing Python modules: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
PY
  then
    echo "❌ required bot Python packages are missing from $PYTHON." >&2
    echo "Install the prerequisites listed in README.md before starting." >&2
    errors=1
  fi

  return "$errors"
}

do_install() {
  if is_loaded; then
    echo "❌ stop ${LABEL} before reinstalling its plist" >&2
    return 1
  fi
  if [[ ! -f "$SCRIPT_DIR/com.dop-agent.plist" || ! -f "$SCRIPT_DIR/dop_agent.py" ]]; then
    echo "❌ bot source or plist template missing under $SCRIPT_DIR" >&2
    return 1
  fi
  if [[ ! -x "$PYTHON" ]]; then
    echo "❌ required interpreter not found: $PYTHON (need Python 3.12+; set DOP_AGENT_PYTHON)" >&2
    return 1
  fi

  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.copilot"
  "$PYTHON" - "$SCRIPT_DIR/com.dop-agent.plist" "$PLIST" "$SCRIPT_DIR" "$HOME" "$PYTHON" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

template, destination, agent_dir, home, python = map(Path, sys.argv[1:])
with template.open("rb") as source:
    plist = plistlib.load(source)

environment = dict(os.environ)
search_path = environment.get("PATH", "")
path_entries = [str(home / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
for entry in search_path.split(os.pathsep):
    if entry and entry not in path_entries:
        path_entries.append(entry)

plist["ProgramArguments"] = [str(python), str(agent_dir / "dop_agent.py")]
plist["WorkingDirectory"] = str(agent_dir)
plist["StandardOutPath"] = str(home / ".copilot" / "dop-agent.log")
plist["StandardErrorPath"] = str(home / ".copilot" / "dop-agent-error.log")
plist["EnvironmentVariables"] = {
    "HOME": str(home),
    "PATH": os.pathsep.join(path_entries),
}
if environment.get("DOP_AGENT_CONFIG"):
    plist["EnvironmentVariables"]["DOP_AGENT_CONFIG"] = environment["DOP_AGENT_CONFIG"]

destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_suffix(destination.suffix + ".tmp")
with temporary.open("wb") as target:
    plistlib.dump(plist, target, fmt=plistlib.FMT_XML, sort_keys=False)
os.chmod(temporary, 0o600)
os.replace(temporary, destination)
PY
  plutil -lint "$PLIST"
  echo "✅ installed launchd plist: $PLIST"
  echo "Run '$0 start' to start the bot."
}

do_start() {
  require_install
  check_runtime
  if is_loaded; then
    local pid
    pid="$(pid_of || true)"
    if [[ -n "${pid:-}" && "$pid" != "-" ]]; then
      echo "✅ ${LABEL} is already running (pid $pid)"
      return 0
    fi
    launchctl kickstart "$SERVICE_TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
  wait_for_pid
}

do_stop() {
  if ! is_loaded; then
    echo "✅ ${LABEL} is already stopped"
    return 0
  fi
  launchctl bootout "$SERVICE_TARGET"
  wait_for_stop
}

# Restart must survive being run FROM the bot (an agent turn is a child of the daemon): `bootout` would kill
# this script before a following `bootstrap`. `kickstart -k` is a single atomic launchd request (kill + start
# done by launchd itself), so it completes even if this process dies.
do_restart() {
  require_install
  check_runtime
  if ! is_loaded; then
    do_start
  else
    local old_pid
    old_pid="$(pid_of || true)"
    echo "restarting ${LABEL} (old pid ${old_pid:-none}) ..."
    launchctl kickstart -k "$SERVICE_TARGET"
    local waited=0 pid
    while (( waited < TIMEOUT_SECS )); do
      pid="$(pid_of || true)"
      if [[ -n "${pid:-}" && "$pid" != "-" && "$pid" != "${old_pid:-}" ]]; then
        echo "✅ ${LABEL} is up (pid $pid) after ${waited}s"
        break
      fi
      sleep 1
      ((waited += 1))
    done
    if (( waited >= TIMEOUT_SECS )); then
      echo "❌ ${LABEL} did not come back up within ${TIMEOUT_SECS}s" >&2
      tail -n 20 "$ERROR_LOG" 2>/dev/null || true
      return 1
    fi
  fi
  echo "--- startup banner (last 15 lines of $ERROR_LOG) ---"
  tail -n 15 "$ERROR_LOG" 2>/dev/null || true
}

do_logs() {
  if [[ ! -f "$ERROR_LOG" ]]; then
    echo "❌ log file not found: $ERROR_LOG" >&2
    return 1
  fi
  tail -f "$ERROR_LOG"
}

# ---- Linux: systemd user unit ----------------------------------------------------------------------------
UNIT_NAME="dop-agent.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$UNIT_NAME"

sd() { systemctl --user "$@"; }

linux_install() {
  if [[ ! -f "$SCRIPT_DIR/scripts/dop-agent.service" || ! -f "$SCRIPT_DIR/dop_agent.py" ]]; then
    echo "❌ bot source or unit template missing under $SCRIPT_DIR" >&2
    return 1
  fi
  if [[ ! -x "$(command -v "$PYTHON" 2>/dev/null || true)" ]]; then
    echo "❌ required interpreter not found: $PYTHON (need Python 3.12+; set DOP_AGENT_PYTHON)" >&2
    return 1
  fi
  local python_abs path_value
  python_abs="$(command -v "$PYTHON")"
  path_value="$HOME/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
  mkdir -p "$UNIT_DIR" "$HOME/.copilot"
  "$PYTHON" - "$SCRIPT_DIR/scripts/dop-agent.service" "$UNIT" "$SCRIPT_DIR" "$HOME" "$python_abs" "$path_value" "${PATH:-}" <<'PY'
import os
import re
import sys
from pathlib import Path

template, destination, agent_dir, home, python, base_path, current_path = sys.argv[1:]
entries = base_path.split(os.pathsep)
entries += [e for e in current_path.split(os.pathsep) if e and e not in entries]
text = Path(template).read_text()
for key, value in {
    "__PYTHON__": python,
    "__DOP_AGENT_DIR__": agent_dir,
    "__HOME__": home,
    "__PATH__": os.pathsep.join(entries),
}.items():
    text = text.replace(key, value)
if re.search(r"__[A-Z_]+__", text):
    raise SystemExit("unreplaced placeholder in unit template")
temporary = Path(destination + ".tmp")
temporary.write_text(text)
os.chmod(temporary, 0o600)
os.replace(temporary, destination)
PY
  sd daemon-reload
  echo "✅ installed systemd user unit: $UNIT"
  echo "Run '$0 start' to start the bot; 'loginctl enable-linger $(id -un)' keeps it running after logout."
}

linux_require_install() {
  if [[ ! -f "$UNIT" ]]; then
    echo "❌ installed unit not found: $UNIT" >&2
    echo "Run: $0 install" >&2
    return 1
  fi
}

linux_start() {
  linux_require_install
  check_runtime
  sd enable "$UNIT_NAME" >/dev/null 2>&1 || true
  sd start "$UNIT_NAME"
  echo "✅ ${UNIT_NAME} started"
}

linux_stop() {
  sd stop "$UNIT_NAME"
  echo "✅ ${UNIT_NAME} stopped"
}

linux_restart() {
  linux_require_install
  check_runtime
  sd restart "$UNIT_NAME"
  echo "✅ ${UNIT_NAME} restarted"
}

linux_status() {
  sd --no-pager status "$UNIT_NAME" || true
}

linux_logs() {
  journalctl --user -u "$UNIT_NAME" -f -n 50
}

if [[ "$OS_NAME" == "Linux" ]]; then
  case "${1:-}" in
    install) linux_install ;;
    start)   linux_start ;;
    stop)    linux_stop ;;
    restart) linux_restart ;;
    status)  linux_status ;;
    logs)    linux_logs ;;
    *)       usage ;;
  esac
  exit $?
fi

case "${1:-}" in
  install) do_install ;;
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_restart ;;
  status)  status ;;
  logs)    do_logs ;;
  *)       usage ;;
esac
