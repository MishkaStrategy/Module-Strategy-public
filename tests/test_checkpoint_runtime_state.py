from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from modular_core.checkpoints import (
    CheckpointManager,
    KernelCheckpoint,
    ModuleState,
    restore_module_states,
)
from modular_core.extensions import InMemoryStateRepository
from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class StatefulModule(StrategyModule):
    def __init__(
        self,
        name: str,
        state: Mapping[str, Any],
        *,
        fail_once: bool = False,
    ) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name, version="1.0.0")
        self.state: dict[str, Any] = dict(state)
        self.fail_once = fail_once

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return self.state

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.state.clear()
        self.state.update(state)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError(f"restore failed for {self.name}")


def _checkpoint(module_states: object = ()) -> KernelCheckpoint:
    return KernelCheckpoint(
        checkpoint_id="checkpoint-1",
        run_id="run-1",
        kernel_version="0.4.2",
        created_at=NOW + timedelta(minutes=1),
        last_bar_index=0,
        last_bar_time=NOW,
        data_hash="data",
        config_hash="config",
        module_hash="module",
        module_states=module_states,  # type: ignore[arg-type]
    )


def test_checkpoint_copies_caller_owned_module_state_sequence() -> None:
    state = ModuleState("module-a", "1.0.0", "1", {"value": 1})
    source = [state]
    checkpoint = _checkpoint(source)
    source.clear()
    assert checkpoint.module_states == (state,)


def test_checkpoint_rejects_invalid_module_state_items() -> None:
    with pytest.raises(TypeError, match="must contain ModuleState"):
        _checkpoint([object()])


def test_restore_rollback_uses_isolated_original_snapshots() -> None:
    first = StatefulModule("first", {"value": "original-first"})
    second = StatefulModule(
        "second",
        {"value": "original-second"},
        fail_once=True,
    )
    states = (
        ModuleState("first", "1.0.0", "1", {"value": "new-first"}),
        ModuleState("second", "1.0.0", "1", {"value": "new-second"}),
    )

    with pytest.raises(RuntimeError, match="restore failed for second"):
        restore_module_states((first, second), states)

    assert first.state == {"value": "original-first"}
    assert second.state == {"value": "original-second"}


def test_restore_rejects_non_boolean_strict_flag() -> None:
    with pytest.raises(TypeError, match="strict must be bool"):
        restore_module_states((), (), strict=1)  # type: ignore[arg-type]


def test_checkpoint_manager_rejects_string_schema_sequence() -> None:
    with pytest.raises(TypeError, match="must be a sequence"):
        CheckpointManager(
            InMemoryStateRepository(),
            supported_schema_versions="1",  # type: ignore[arg-type]
        )


def test_checkpoint_load_does_not_coerce_last_bar_index() -> None:
    repository = InMemoryStateRepository()
    repository.save(
        "checkpoint",
        {
            "checkpoint_id": "checkpoint-1",
            "run_id": "run-1",
            "kernel_version": "0.4.2",
            "created_at": (NOW + timedelta(minutes=1)).isoformat(),
            "last_bar_index": "0",
            "last_bar_time": NOW.isoformat(),
            "data_hash": "data",
            "config_hash": "config",
            "module_hash": "module",
            "module_states": (),
            "schema_version": "1",
        },
    )
    manager = CheckpointManager(repository)
    with pytest.raises(TypeError, match="last_bar_index must be int"):
        manager.load("checkpoint")


def test_checkpoint_load_rejects_malformed_state_collection() -> None:
    repository = InMemoryStateRepository()
    repository.save(
        "checkpoint",
        {
            "checkpoint_id": "checkpoint-1",
            "run_id": "run-1",
            "kernel_version": "0.4.2",
            "created_at": (NOW + timedelta(minutes=1)).isoformat(),
            "last_bar_index": 0,
            "last_bar_time": NOW.isoformat(),
            "data_hash": "data",
            "config_hash": "config",
            "module_hash": "module",
            "module_states": "not-a-sequence-of-states",
            "schema_version": "1",
        },
    )
    manager = CheckpointManager(repository)
    with pytest.raises(TypeError, match="module_states must be a sequence"):
        manager.load("checkpoint")


def test_checkpoint_created_at_cannot_precede_runtime_cursor() -> None:
    with pytest.raises(ValueError, match="cannot precede"):
        KernelCheckpoint(
            checkpoint_id="checkpoint-1",
            run_id="run-1",
            kernel_version="0.4.2",
            created_at=NOW,
            last_bar_index=1,
            last_bar_time=NOW + timedelta(minutes=1),
            data_hash="data",
            config_hash="config",
            module_hash="module",
            module_states=(),
        )
