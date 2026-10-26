from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Mapping

import pytest

from modular_core.checkpoints import (
    CheckpointManager,
    KernelCheckpoint,
    build_checkpoint,
)
from modular_core.extensions import InMemoryStateRepository
from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.serialization import to_jsonable


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SnapshotProbe(StrategyModule):
    def __init__(self, *, value: int = 1) -> None:
        self.name = "snapshot-probe"
        self.manifest = ModuleManifest(
            name=self.name,
            version="1.0.0",
            state_schema_version="1",
        )
        self.value = value
        self.snapshot_calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        self.snapshot_calls += 1
        return {"value": self.value}


def _checkpoint(
    module: SnapshotProbe,
    **overrides: object,
) -> KernelCheckpoint:
    values: dict[str, object] = {
        "run_id": "run-1",
        "kernel_version": "0.4.2",
        "created_at": NOW + timedelta(minutes=1),
        "last_bar_index": 10,
        "last_bar_time": NOW,
        "data_hash": "data-v1",
        "config_hash": "config-v1",
        "module_hash": "modules-v1",
        "modules": (module,),
    }
    values.update(overrides)
    return build_checkpoint(**values)  # type: ignore[arg-type]


def _payload(checkpoint: KernelCheckpoint) -> dict[str, object]:
    payload = to_jsonable(checkpoint)
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", 1, "checkpoint run_id must be str"),
        ("kernel_version", "", "checkpoint kernel_version is required"),
        ("created_at", "2026-01-01", "checkpoint created_at must be datetime"),
        ("last_bar_index", True, "checkpoint last_bar_index must be int"),
        ("last_bar_time", "2026-01-01", "checkpoint last_bar_time must be datetime"),
        ("data_hash", "", "checkpoint data_hash is required"),
        ("config_hash", "", "checkpoint config_hash is required"),
        ("module_hash", "", "checkpoint module_hash is required"),
    ],
)
def test_build_checkpoint_validates_static_inputs_before_snapshot(
    field: str,
    value: object,
    message: str,
) -> None:
    module = SnapshotProbe()

    with pytest.raises((TypeError, ValueError), match=message):
        _checkpoint(module, **{field: value})

    assert module.snapshot_calls == 0


def test_checkpoint_identity_uses_canonical_utc_time() -> None:
    offset = timezone(timedelta(hours=3))
    utc_module = SnapshotProbe()
    offset_module = SnapshotProbe()

    utc_checkpoint = _checkpoint(utc_module)
    offset_checkpoint = _checkpoint(
        offset_module,
        created_at=(NOW + timedelta(minutes=1)).astimezone(offset),
        last_bar_time=NOW.astimezone(offset),
    )

    assert utc_checkpoint.last_bar_time == offset_checkpoint.last_bar_time
    assert utc_checkpoint.created_at == offset_checkpoint.created_at
    assert utc_checkpoint.checkpoint_id == offset_checkpoint.checkpoint_id
    assert utc_checkpoint.integrity_sha256 == offset_checkpoint.integrity_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run-2"),
        ("kernel_version", "0.4.3"),
        ("last_bar_index", 11),
        ("data_hash", "data-v2"),
        ("config_hash", "config-v2"),
        ("module_hash", "modules-v2"),
    ],
)
def test_checkpoint_identity_changes_with_runtime_identity(
    field: str,
    value: object,
) -> None:
    baseline = _checkpoint(SnapshotProbe())
    changed = _checkpoint(SnapshotProbe(), **{field: value})

    assert baseline.checkpoint_id != changed.checkpoint_id


def test_checkpoint_identity_changes_with_module_state() -> None:
    baseline = _checkpoint(SnapshotProbe(value=1))
    changed = _checkpoint(SnapshotProbe(value=2))

    assert baseline.checkpoint_id != changed.checkpoint_id


@pytest.mark.parametrize("value", [True, 1, object()])
def test_checkpoint_integrity_requires_string_or_none(value: object) -> None:
    checkpoint = _checkpoint(SnapshotProbe())

    with pytest.raises(TypeError, match="integrity_sha256 must be str or None"):
        replace(checkpoint, integrity_sha256=value)  # type: ignore[arg-type]


def test_checkpoint_manager_rejects_invalid_integrity_before_persisting() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    checkpoint = _checkpoint(SnapshotProbe())
    tampered = replace(checkpoint, integrity_sha256="0" * 64)

    with pytest.raises(ValueError, match="integrity verification failed"):
        manager.save("checkpoint", tampered)

    assert repository.load("checkpoint") is None


def test_checkpoint_manager_rejects_unsupported_schema_before_persisting() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    checkpoint = replace(
        _checkpoint(SnapshotProbe()),
        schema_version="2",
        integrity_sha256=None,
    )

    with pytest.raises(ValueError, match="unsupported checkpoint schema: 2"):
        manager.save("checkpoint", checkpoint)

    assert repository.load("checkpoint") is None


def test_checkpoint_manager_round_trips_verified_checkpoint() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    checkpoint = _checkpoint(SnapshotProbe())

    manager.save("checkpoint", checkpoint)

    assert manager.load("checkpoint") == checkpoint


@pytest.mark.parametrize(
    "field",
    [
        "checkpoint_id",
        "run_id",
        "kernel_version",
        "created_at",
        "last_bar_index",
        "last_bar_time",
        "data_hash",
        "config_hash",
        "module_hash",
    ],
)
def test_checkpoint_load_reports_missing_required_root_field(field: str) -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    payload = _payload(_checkpoint(SnapshotProbe()))
    payload.pop(field)
    repository.save("checkpoint", payload)

    with pytest.raises(
        ValueError,
        match=f"checkpoint payload missing required field: {field}",
    ):
        manager.load("checkpoint")


@pytest.mark.parametrize("field", ["module", "module_version", "schema_version"])
def test_checkpoint_load_reports_missing_required_module_state_field(
    field: str,
) -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    payload = _payload(_checkpoint(SnapshotProbe()))
    payload["integrity_sha256"] = None
    states = payload["module_states"]
    assert isinstance(states, list)
    row = states[0]
    assert isinstance(row, dict)
    row.pop(field)
    repository.save("checkpoint", payload)

    with pytest.raises(
        ValueError,
        match=rf"checkpoint module_states\[0\] missing required field: {field}",
    ):
        manager.load("checkpoint")


def test_checkpoint_load_rejects_non_string_integrity_type() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    payload = _payload(_checkpoint(SnapshotProbe()))
    payload["integrity_sha256"] = 1
    repository.save("checkpoint", payload)

    with pytest.raises(TypeError, match="integrity_sha256 must be str or None"):
        manager.load("checkpoint")
