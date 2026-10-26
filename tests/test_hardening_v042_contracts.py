from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modular_core.checkpoints import CheckpointManager, KernelCheckpoint, ModuleState
from modular_core.events import EventEnvelope
from modular_core.extensions import InMemoryStateRepository

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(**overrides: object) -> EventEnvelope:
    values: dict[str, object] = {
        "event_id": "event-1",
        "run_id": "run-1",
        "sequence": 1,
        "timestamp": NOW,
        "event_type": "test.event",
        "source": "tests",
        "payload": {},
    }
    values.update(overrides)
    return EventEnvelope(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("sequence", [True, 1.5, "1"])
def test_round05_event_sequence_requires_exact_integer(sequence: object) -> None:
    with pytest.raises(TypeError, match="sequence must be int"):
        _event(sequence=sequence)


@pytest.mark.parametrize("field", ["correlation_id", "causation_id", "symbol"])
def test_round05_optional_event_identifiers_reject_blank(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _event(**{field: "  "})


def test_round05_event_payload_requires_mapping() -> None:
    with pytest.raises(TypeError, match="payload must be a mapping"):
        _event(payload=[("value", 1)])


def _checkpoint(**overrides: object) -> KernelCheckpoint:
    values: dict[str, object] = {
        "checkpoint_id": "checkpoint-1",
        "run_id": "run-1",
        "kernel_version": "0.4.2",
        "created_at": NOW,
        "last_bar_index": 0,
        "last_bar_time": NOW,
        "data_hash": "data",
        "config_hash": "config",
        "module_hash": "module",
        "module_states": (),
    }
    values.update(overrides)
    return KernelCheckpoint(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_round06_checkpoint_index_requires_exact_integer(value: object) -> None:
    with pytest.raises(TypeError, match="last_bar_index must be int"):
        _checkpoint(last_bar_index=value)


def test_round06_module_state_payload_requires_mapping() -> None:
    with pytest.raises(TypeError, match="payload must be a mapping"):
        ModuleState("module", "1.0.0", "1", [1, 2])  # type: ignore[arg-type]


def test_round06_supported_checkpoint_schema_rejects_surrounding_whitespace() -> None:
    with pytest.raises(ValueError, match="canonical nonblank"):
        CheckpointManager(InMemoryStateRepository(), supported_schema_versions=(" 1",))


def test_round06_checkpoint_load_does_not_coerce_schema_type() -> None:
    repository = InMemoryStateRepository()
    repository.save(
        "checkpoint",
        {
            "checkpoint_id": "checkpoint-1",
            "run_id": "run-1",
            "kernel_version": "0.4.2",
            "created_at": NOW.isoformat(),
            "last_bar_index": 0,
            "last_bar_time": NOW.isoformat(),
            "data_hash": "data",
            "config_hash": "config",
            "module_hash": "module",
            "module_states": (),
            "schema_version": 1,
        },
    )
    with pytest.raises(TypeError, match="schema_version must be str"):
        CheckpointManager(repository).load("checkpoint")
