from __future__ import annotations

from pathlib import Path

import pytest

from scripts import system_acceptance_stage1_systemd as systemd_module
from scripts.system_acceptance_stage1_systemd import (
    Stage1SystemdServiceConfig,
    install_stage1_systemd_service,
    render_stage1_systemd_service,
)


def _config(tmp_path: Path) -> Stage1SystemdServiceConfig:
    working = tmp_path / "deployment checkout"
    working.mkdir()
    state_directory = tmp_path / "private state"
    state_directory.mkdir()
    executable = tmp_path / "deployment bin" / "stage1-owner"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    return Stage1SystemdServiceConfig(
        unit_directory=tmp_path / "units",
        executable_path=executable,
        working_directory=working,
        state_directory=state_directory,
        state_path=state_directory / "acceptance.sqlite3",
        user="stage1-owner",
        group="stage1-group",
    )


def test_renderer_uses_fixed_no_argument_paper_unit_with_private_state(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    payload = render_stage1_systemd_service(config).decode("utf-8")

    assert "[Service]" in payload
    assert "Type=simple" in payload
    assert "User=stage1-owner" in payload
    assert "Group=stage1-group" in payload
    assert f'WorkingDirectory="{config.working_directory}"' in payload
    assert f'ExecStart="{config.executable_path}"' in payload
    assert "UMask=0077" in payload
    assert "Restart=on-failure" in payload
    assert "ProtectSystem=strict" in payload
    assert "KillMode=control-group" in payload
    assert "SendSIGKILL=yes" in payload
    assert "StandardOutput=journal" in payload
    assert "StandardError=journal" in payload
    assert f'ReadWritePaths="{config.state_directory}"' in payload
    assert "Environment=" not in payload
    assert "/bin/sh" not in payload
    assert "--" not in payload


def test_renderer_escapes_systemd_special_path_characters(tmp_path: Path) -> None:
    special_component = 'deployment%n"\\checkout'
    deployment_root = tmp_path / special_component
    working = deployment_root / "working"
    state_directory = deployment_root / "state"
    executable = deployment_root / "bin" / "stage1-owner"
    working.mkdir(parents=True)
    state_directory.mkdir()
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    config = Stage1SystemdServiceConfig(
        unit_directory=tmp_path / "units",
        executable_path=executable,
        working_directory=working,
        state_directory=state_directory,
        state_path=state_directory / "acceptance.sqlite3",
        user="stage1-owner",
        group="stage1-group",
    )

    lines = render_stage1_systemd_service(config).decode("utf-8").splitlines()

    # systemd.syntax treats %% as a literal percent; quoted tokens also need
    # embedded double quotes and backslashes escaped.
    escaped_component = 'deployment%%n\\"\\\\checkout'
    assert f'WorkingDirectory="{tmp_path}/{escaped_component}/working"' in lines
    assert f'ExecStart="{tmp_path}/{escaped_component}/bin/stage1-owner"' in lines
    assert f'ReadWritePaths="{tmp_path}/{escaped_component}/state"' in lines


def test_installer_atomically_writes_fixed_unit_with_mode_0600(tmp_path: Path) -> None:
    config = _config(tmp_path)

    installed = install_stage1_systemd_service(config)

    assert installed == config.unit_directory / "module-strategy-stage1.service"
    assert installed.read_bytes() == render_stage1_systemd_service(config)
    assert installed.stat().st_mode & 0o777 == 0o600


def test_failed_replace_preserves_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.unit_directory.mkdir()
    original = config.unit_path
    original.write_bytes(b"existing unit\n")

    def fail_replace(source: Path, destination: Path) -> None:
        assert source.exists()
        assert destination == original
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(systemd_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated atomic replacement failure"):
        install_stage1_systemd_service(config)

    assert original.read_bytes() == b"existing unit\n"
    assert not list(config.unit_directory.glob("*.tmp"))


def test_installer_rejects_symlink_destination_without_following_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.unit_directory.mkdir()
    victim = tmp_path / "existing"
    victim.write_bytes(b"keep")
    config.unit_path.symlink_to(victim)

    with pytest.raises(ValueError, match="must not be a symlink"):
        install_stage1_systemd_service(config)

    assert victim.read_bytes() == b"keep"
    assert config.unit_path.is_symlink()


@pytest.mark.parametrize("identity", ("", "-owner", "owner;id", "bad/name", "owner\nGroup=root"))
def test_config_rejects_invalid_user_and_group(identity: str, tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="explicit valid account name or numeric ID"):
        Stage1SystemdServiceConfig(
            unit_directory=config.unit_directory,
            executable_path=config.executable_path,
            working_directory=config.working_directory,
            state_directory=config.state_directory,
            state_path=config.state_path,
            user=identity,
            group=config.group,
        )


def test_config_rejects_relative_and_parent_traversing_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="absolute Path"):
        Stage1SystemdServiceConfig(
            unit_directory=config.unit_directory,
            executable_path=Path("relative-executable"),
            working_directory=config.working_directory,
            state_directory=config.state_directory,
            state_path=config.state_path,
            user=config.user,
            group=config.group,
        )
    with pytest.raises(ValueError, match="parent traversal"):
        Stage1SystemdServiceConfig(
            unit_directory=config.unit_directory,
            executable_path=config.executable_path,
            working_directory=tmp_path / "checkout" / ".." / "checkout",
            state_directory=config.state_directory,
            state_path=config.state_path,
            user=config.user,
            group=config.group,
        )


def test_config_rejects_state_paths_outside_the_explicit_state_directory(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    unrelated_directory = tmp_path / "unrelated state"
    unrelated_directory.mkdir()
    nested_directory = config.state_directory / "nested"
    nested_directory.mkdir()

    for state_path in (
        unrelated_directory / "acceptance.sqlite3",
        nested_directory / "acceptance.sqlite3",
    ):
        with pytest.raises(ValueError, match="directly within state_directory"):
            Stage1SystemdServiceConfig(
                unit_directory=config.unit_directory,
                executable_path=config.executable_path,
                working_directory=config.working_directory,
                state_directory=config.state_directory,
                state_path=state_path,
                user=config.user,
                group=config.group,
            )


def test_config_rejects_symlink_executable_and_unit_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    real_executable = config.executable_path
    linked_executable = tmp_path / "linked-executable"
    linked_executable.symlink_to(real_executable)
    with pytest.raises(ValueError, match="executable_path must not be a symlink"):
        Stage1SystemdServiceConfig(
            unit_directory=config.unit_directory,
            executable_path=linked_executable,
            working_directory=config.working_directory,
            state_directory=config.state_directory,
            state_path=config.state_path,
            user=config.user,
            group=config.group,
        )

    config.unit_directory.symlink_to(tmp_path / "linked-units")
    with pytest.raises(ValueError, match="unit_directory must not be a symlink"):
        Stage1SystemdServiceConfig(
            unit_directory=config.unit_directory,
            executable_path=config.executable_path,
            working_directory=config.working_directory,
            state_directory=config.state_directory,
            state_path=config.state_path,
            user=config.user,
            group=config.group,
        )
