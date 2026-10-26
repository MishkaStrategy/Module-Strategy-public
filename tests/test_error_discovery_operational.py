from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

import modular_core.extensions as extensions
from modular_core.checkpoints import ModuleState, restore_module_states
from modular_core.extensions import (
    AtomicJsonStateRepository,
    InMemoryStateRepository,
)
from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule


class RollbackProbe(StrategyModule):
    def __init__(
        self,
        name: str,
        *,
        fail_on: int | None = None,
        fail_after_mutation: bool = False,
    ) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name, version="1.0.0", state_schema_version="1")
        self.value = 0
        self.fail_on = fail_on
        self.fail_after_mutation = fail_after_mutation

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"value": self.value}

    def restore_state(self, state: Mapping[str, object]) -> None:
        value = int(state["value"])
        if self.fail_on == value and not self.fail_after_mutation:
            raise RuntimeError(f"{self.name} rejected {value}")
        self.value = value
        if self.fail_on == value and self.fail_after_mutation:
            raise RuntimeError(f"{self.name} failed after setting {value}")


def test_atomic_state_save_cleans_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = AtomicJsonStateRepository(tmp_path)

    def fail_replace(source: str | Path, target: str | Path) -> None:
        raise OSError(f"replace failed: {source} -> {target}")

    monkeypatch.setattr(extensions.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        repository.save("state", {"value": 1})

    assert list(tmp_path.iterdir()) == []


def test_incomplete_restore_rollback_is_a_hard_failure() -> None:
    first = RollbackProbe("first", fail_on=10)
    second = RollbackProbe("second", fail_on=2, fail_after_mutation=True)
    first.value = 10
    second.value = 20
    states = (
        ModuleState("first", "1.0.0", "1", {"value": 1}),
        ModuleState("second", "1.0.0", "1", {"value": 2}),
    )

    with pytest.raises(RuntimeError, match="rollback was incomplete") as captured:
        restore_module_states((first, second), states)

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert "second failed after setting 2" in str(captured.value.__cause__)
    assert second.value == 20
    assert first.value == 1  # rollback failure is surfaced; state is not misreported as atomic


def test_corrupt_or_oversized_disk_state_fails_closed(tmp_path: Path) -> None:
    repository = AtomicJsonStateRepository(tmp_path, max_state_bytes=64)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        repository.load("corrupt")

    oversized = tmp_path / "oversized.json"
    oversized.write_text(json.dumps({"payload": "x" * 100}), encoding="utf-8")
    with pytest.raises(ValueError, match="size limit"):
        repository.load("oversized")


def test_state_repository_instances_do_not_share_state(tmp_path: Path) -> None:
    left = AtomicJsonStateRepository(tmp_path / "left")
    right = AtomicJsonStateRepository(tmp_path / "right")
    left.save("state", {"value": 1})
    right.save("state", {"value": 2})
    assert left.load("state")["value"] == 1  # type: ignore[index]
    assert right.load("state")["value"] == 2  # type: ignore[index]


def test_state_repository_rejects_non_integer_size_limits(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="max_state_bytes"):
        InMemoryStateRepository(max_state_bytes=True)
    with pytest.raises(TypeError, match="max_state_bytes"):
        AtomicJsonStateRepository(tmp_path, max_state_bytes=1.5)  # type: ignore[arg-type]


def test_atomic_state_load_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"value": 1}', encoding="utf-8")
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    repository = AtomicJsonStateRepository(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        repository.load("alias")


def test_atomic_state_load_enforces_limit_after_fstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = AtomicJsonStateRepository(tmp_path, max_state_bytes=32)
    target = tmp_path / "growing.json"
    target.write_text(json.dumps({"payload": "x" * 100}), encoding="utf-8")
    real_fstat = extensions.os.fstat

    def underreport_size(descriptor: int) -> SimpleNamespace:
        metadata = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_size=1,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
        )

    monkeypatch.setattr(extensions.os, "fstat", underreport_size)
    with pytest.raises(ValueError, match="size limit"):
        repository.load("growing")
