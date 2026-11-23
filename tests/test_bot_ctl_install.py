"""The launchd plist must be generated for this checkout, not another user's path.

Installing derives paths from the current account and checkout and keeps
credentials in the bot's dotenv configuration rather than the launchd plist.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bot_ctl.sh"
PYTHON = Path(sys.executable)


class BotCtlInstallTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "launchd plist is macOS-only")
    def test_install_generates_current_paths_without_persisting_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            fake_launchctl = fake_bin / "launchctl"
            fake_launchctl.write_text("#!/bin/sh\nexit 1\n")
            fake_launchctl.chmod(0o755)
            env = dict(
                os.environ, HOME=str(home), PATH=f"{fake_bin}:{os.environ['PATH']}", DOP_AGENT_PYTHON=str(PYTHON)
            )

            subprocess.run(
                [str(SCRIPT), "install"],
                check=True,
                cwd=SCRIPT.parent,
                env=env,
                capture_output=True,
                text=True,
            )

            installed = home / "Library/LaunchAgents/com.dop-agent.plist"
            with installed.open("rb") as stream:
                plist = plistlib.load(stream)

            self.assertEqual(
                plist["ProgramArguments"],
                [str(PYTHON), str(SCRIPT.parent / "dop_agent.py")],
            )
            self.assertEqual(plist["WorkingDirectory"], str(SCRIPT.parent))
            self.assertEqual(plist["EnvironmentVariables"]["HOME"], str(home))
            self.assertNotIn("COPILOT_GITHUB_TOKEN", plist["EnvironmentVariables"])
            self.assertEqual(installed.stat().st_mode & 0o777, 0o600)


    @unittest.skipUnless(sys.platform.startswith("linux"), "systemd user unit is Linux-only")
    def test_linux_install_renders_systemd_user_unit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text("#!/bin/sh\nexit 0\n")
            fake_systemctl.chmod(0o755)
            env = dict(
                os.environ,
                HOME=str(home),
                XDG_CONFIG_HOME=str(home / ".config"),
                PATH=f"{fake_bin}:{os.environ['PATH']}",
                DOP_AGENT_PYTHON=str(PYTHON),
            )

            subprocess.run(
                [str(SCRIPT), "install"], check=True, cwd=SCRIPT.parent, env=env, capture_output=True, text=True
            )

            unit = home / ".config/systemd/user/dop-agent.service"
            text = unit.read_text()
            self.assertIn(f"ExecStart={PYTHON} {SCRIPT.parent / 'dop_agent.py'}", text)
            self.assertIn(f"WorkingDirectory={SCRIPT.parent}", text)
            self.assertIn(f"Environment=HOME={home}", text)
            self.assertNotIn("__", text)
            self.assertNotIn("COPILOT_GITHUB_TOKEN", text)
            self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
