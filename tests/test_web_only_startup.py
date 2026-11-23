"""The Web UI must remain startable when no chat adapter has credentials:
importing bot_config must not require Slack tokens.
"""

import os
import subprocess
import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1]


def test_web_ui_configuration_loads_without_slack_adapter_credentials(tmp_path):
    env = dict(os.environ)
    env["SLACK_BOT_TOKEN"] = ""
    env["SLACK_APP_TOKEN"] = ""
    env["DOP_AGENT_CONFIG"] = str(tmp_path / "missing-config.yaml")

    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import dop_agent; "
                "assert dop_agent.SLACK_DISABLED; "
                "assert dop_agent.app is None; "
                "assert dop_agent.web_server.app is not None"
            ),
        ],
        check=True,
        cwd=BOT_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
