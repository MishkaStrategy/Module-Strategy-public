"""Safe user LaunchAgent handoff for a fixed Stage-1 manager executable."""

from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path

_LABEL = "com.mishkastrategy.module-strategy.stage1"


@dataclass(frozen=True, slots=True)
class Stage1LaunchAgentConfig:
    """Deployment-owned absolute paths; no command arguments or secrets."""

    user_home: Path = field(repr=False)
    checkout_path: Path = field(repr=False)
    executable_path: Path = field(repr=False)
    stdout_path: Path = field(repr=False)
    stderr_path: Path = field(repr=False)
    launch_agents_directory: Path = field(repr=False)

    def __post_init__(self) -> None:
        for value, name in (
            (self.user_home, "user_home"),
            (self.checkout_path, "checkout_path"),
            (self.executable_path, "executable_path"),
            (self.stdout_path, "stdout_path"),
            (self.stderr_path, "stderr_path"),
            (self.launch_agents_directory, "launch_agents_directory"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
            if value.is_symlink():
                raise ValueError(f"{name} must not be a symlink")
        if not self.checkout_path.is_dir():
            raise ValueError("checkout_path must be an existing directory")
        if not self.user_home.is_dir():
            raise ValueError("user_home must be an existing directory")
        if not self.executable_path.is_file() or not os.access(self.executable_path, os.X_OK):
            raise ValueError("executable_path must be an existing executable file")
        if self.launch_agents_directory != self.user_home / "Library" / "LaunchAgents":
            raise ValueError("launch_agents_directory must belong to the configured user home")
        for log_path in (self.stdout_path, self.stderr_path):
            if log_path == self.executable_path or log_path == self.checkout_path:
                raise ValueError("launchd log paths must be dedicated files")

    @property
    def plist_path(self) -> Path:
        return self.launch_agents_directory / f"{_LABEL}.plist"


def render_stage1_launch_agent(config: Stage1LaunchAgentConfig) -> bytes:
    """Render one non-shell LaunchAgent with no token-bearing argv/environment."""

    if not isinstance(config, Stage1LaunchAgentConfig):
        raise TypeError("config must be Stage1LaunchAgentConfig")
    payload = {
        "Label": _LABEL,
        "ProgramArguments": [str(config.executable_path)],
        "WorkingDirectory": str(config.checkout_path),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(config.stdout_path),
        "StandardErrorPath": str(config.stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def install_stage1_launch_agent(config: Stage1LaunchAgentConfig) -> Path:
    """Atomically install the template; launchctl activation remains an operator action."""

    target = config.plist_path
    parent = config.launch_agents_directory
    if parent.is_symlink():
        raise ValueError("LaunchAgents directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError("LaunchAgent plist must not be a symlink")
    for log_path in (config.stdout_path, config.stderr_path):
        if log_path.parent.is_symlink():
            raise ValueError("launchd log directory must not be a symlink")
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(render_stage1_launch_agent(config))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return target


__all__ = [
    "Stage1LaunchAgentConfig",
    "install_stage1_launch_agent",
    "render_stage1_launch_agent",
]
