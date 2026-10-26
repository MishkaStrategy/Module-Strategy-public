from __future__ import annotations

import plistlib
from pathlib import Path

from scripts.system_acceptance_stage1_launchd import (
    Stage1LaunchAgentConfig,
    install_stage1_launch_agent,
    render_stage1_launch_agent,
)


def _config(tmp_path: Path) -> Stage1LaunchAgentConfig:
    checkout = tmp_path / "Module-Strategy-stage1"
    checkout.mkdir()
    executable = tmp_path / "bin" / "module-strategy-stage1"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return Stage1LaunchAgentConfig(
        user_home=tmp_path,
        checkout_path=checkout,
        executable_path=executable,
        stdout_path=tmp_path / "logs" / "stage1.stdout.log",
        stderr_path=tmp_path / "logs" / "stage1.stderr.log",
        launch_agents_directory=tmp_path / "Library" / "LaunchAgents",
    )


def test_launch_agent_is_user_scoped_non_shell_and_secret_free(tmp_path: Path) -> None:
    config = _config(tmp_path)

    payload = plistlib.loads(render_stage1_launch_agent(config))

    assert payload["Label"] == "com.mishkastrategy.module-strategy.stage1"
    assert payload["ProgramArguments"] == [str(config.executable_path)]
    assert payload["WorkingDirectory"] == str(config.checkout_path)
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert "EnvironmentVariables" not in payload
    assert "token" not in render_stage1_launch_agent(config).decode().lower()
    assert "/bin/sh" not in payload["ProgramArguments"]


def test_launch_agent_installs_atomically_with_private_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)

    installed = install_stage1_launch_agent(config)

    assert installed == config.plist_path
    assert installed.is_file()
    assert installed.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(installed.read_bytes())["WorkingDirectory"] == str(config.checkout_path)
