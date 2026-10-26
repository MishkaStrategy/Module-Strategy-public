"""Systemd unit renderer and installer groundwork for Stage-1 PAPER acceptance.

This module only renders and atomically installs a unit into a caller-selected
directory. It does not activate the unit or claim support for a Linux image;
owner-selected Linux image and clean-host acceptance remain required.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_UNIT_NAME = "module-strategy-stage1.service"
_IDENTITY = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.-]{0,63}|[0-9]{1,10})\Z")


@dataclass(frozen=True, slots=True)
class Stage1SystemdServiceConfig:
    """Deployment-owned paths and explicit service identity for one fixed unit."""

    unit_directory: Path = field(repr=False)
    executable_path: Path = field(repr=False)
    working_directory: Path = field(repr=False)
    state_directory: Path = field(repr=False)
    state_path: Path = field(repr=False)
    user: str
    group: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.unit_directory, "unit_directory"),
            (self.executable_path, "executable_path"),
            (self.working_directory, "working_directory"),
            (self.state_directory, "state_directory"),
            (self.state_path, "state_path"),
        ):
            _validate_path(value, name)
        if not self.executable_path.is_file() or not os.access(self.executable_path, os.X_OK):
            raise ValueError("executable_path must be an existing executable file")
        if not self.working_directory.is_dir():
            raise ValueError("working_directory must be an existing directory")
        if not self.state_directory.is_dir():
            raise ValueError("state_directory must be an existing directory")
        if self.state_path.parent != self.state_directory:
            raise ValueError("state_path must be a file directly within state_directory")
        if self.state_path.exists() and not self.state_path.is_file():
            raise ValueError("state_path must be a regular file when it exists")
        if self.unit_directory.exists() and not self.unit_directory.is_dir():
            raise ValueError("unit_directory must be a directory")
        _validate_identity(self.user, "user")
        _validate_identity(self.group, "group")

    @property
    def unit_path(self) -> Path:
        return self.unit_directory / _UNIT_NAME


def render_stage1_systemd_service(config: Stage1SystemdServiceConfig) -> bytes:
    """Render a fixed, no-argument service with private state and journald output."""

    if not isinstance(config, Stage1SystemdServiceConfig):
        raise TypeError("config must be Stage1SystemdServiceConfig")
    lines = (
        "[Unit]",
        "Description=MishkaStrategy Stage-1 System Acceptance (PAPER)",
        "",
        "[Service]",
        "Type=simple",
        f"User={config.user}",
        f"Group={config.group}",
        f"WorkingDirectory={_quote_systemd(str(config.working_directory))}",
        f"ExecStart={_quote_systemd(str(config.executable_path))}",
        "UMask=0077",
        "Restart=on-failure",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "StandardOutput=journal",
        "StandardError=journal",
        "ProtectSystem=strict",
        # SQLite WAL mode creates sibling -wal and -shm files beside the DB.
        f"ReadWritePaths={_quote_systemd(str(config.state_directory))}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    )
    return "\n".join(lines).encode("utf-8")


def install_stage1_systemd_service(config: Stage1SystemdServiceConfig) -> Path:
    """Atomically install a private unit file; service activation is out of scope."""

    if not isinstance(config, Stage1SystemdServiceConfig):
        raise TypeError("config must be Stage1SystemdServiceConfig")
    parent = config.unit_directory
    if parent.is_symlink():
        raise ValueError("unit_directory must not be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("unit_directory must be a real directory")

    target = config.unit_path
    if target.is_symlink():
        raise ValueError("systemd unit path must not be a symlink")
    if target.exists() and not target.is_file():
        raise ValueError("systemd unit destination must be a regular file")

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.is_symlink():
        raise ValueError("temporary systemd unit path must not be a symlink")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(render_stage1_systemd_service(config))
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        if parent.is_symlink() or target.is_symlink():
            raise ValueError("systemd unit destination changed during installation")
        os.replace(temporary, target)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return target


def _validate_path(value: Path, name: str) -> None:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{name} must be an absolute Path")
    if ".." in value.parts:
        raise ValueError(f"{name} must not contain parent traversal")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in str(value)):
        raise ValueError(f"{name} must not contain control characters")
    if value.is_symlink():
        raise ValueError(f"{name} must not be a symlink")


def _validate_identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError(f"{name} must be an explicit valid account name or numeric ID")


def _quote_systemd(value: str) -> str:
    """Quote one systemd token and suppress specifier expansion in deployment paths."""

    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


__all__ = [
    "Stage1SystemdServiceConfig",
    "install_stage1_systemd_service",
    "render_stage1_systemd_service",
]
