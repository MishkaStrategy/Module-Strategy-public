from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path

import pytest

from modular_core.checkpoints import (
    CheckpointManager,
    ModuleState,
    build_checkpoint,
    restore_module_states,
)
from modular_core.events import EventEnvelope, InMemoryEventStore, JsonLinesEventStore
from modular_core.extensions import AtomicJsonStateRepository, InMemoryStateRepository
from modular_core.features import Feature, FeatureStore, FeatureView
from modular_core.manifest import (
    ModuleConfigurationError,
    ModuleManifest,
    ModuleRegistry,
    validate_parameters,
)
from modular_core.module_api import StrategyModule
from modular_core.replay import verify_event_stream
from modular_core.serialization import to_jsonable

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def event(sequence: int, *, run_id: str = "run", event_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or f"e{sequence}",
        run_id=run_id,
        sequence=sequence,
        timestamp=NOW,
        event_type="test",
        source="tests",
        payload={"sequence": sequence},
    )


def test_event_envelope_rejects_invalid_identity() -> None:
    with pytest.raises(ValueError, match="positive"):
        event(0)
    with pytest.raises(ValueError, match="cause itself"):
        EventEnvelope("same", "run", 1, NOW, "x", "y", {}, causation_id="same")


def test_event_store_rejects_mixed_run_id() -> None:
    store = InMemoryEventStore()
    store.append(event(1))
    with pytest.raises(ValueError, match="mixed run_id"):
        store.append(event(2, run_id="other"))


def test_replay_reports_sequence_gap() -> None:
    report = verify_event_stream((event(1), event(3)))
    assert not report.valid
    assert any("sequence_gap" in row for row in report.errors)


def test_jsonl_oversized_append_does_not_mutate_memory(tmp_path: Path) -> None:
    store = JsonLinesEventStore(tmp_path / "events.jsonl", max_line_bytes=80)
    oversized = EventEnvelope("e1", "run", 1, NOW, "x", "tests", {"blob": "x" * 100})
    with pytest.raises(ValueError, match="max_line_bytes"):
        store.append(oversized)
    assert store.read() == ()
    assert not (tmp_path / "events.jsonl").exists()


def test_jsonl_reports_line_number_for_oversized_state(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        JsonLinesEventStore(path, max_line_bytes=10)


def test_state_repositories_deep_freeze_and_reject_path_escape(tmp_path: Path) -> None:
    memory = InMemoryStateRepository()
    source = {"nested": {"value": [1, 2]}}
    memory.save("safe", source)
    source["nested"]["value"].append(3)
    assert memory.load("safe")["nested"]["value"] == (1, 2)
    with pytest.raises(ValueError, match="safe relative"):
        memory.save("../escape", {})

    disk = AtomicJsonStateRepository(tmp_path, max_state_bytes=50)
    with pytest.raises(ValueError, match="safe relative"):
        disk.save("a/b", {})
    with pytest.raises(ValueError, match="size limit"):
        disk.save("large", {"payload": "x" * 100})


def test_feature_validation_capacity_and_failed_publish_does_not_consume_id() -> None:
    with pytest.raises(ValueError, match="namespace segments"):
        Feature("bad..key", "producer", NOW, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        FeatureView((), datetime(2026, 1, 1))

    store = FeatureStore(max_features=1)
    first = store.publish(Feature("x.value", "producer", NOW, 1))
    assert first.feature_id == "feat-00000001"
    with pytest.raises(ValueError, match="capacity"):
        store.publish(Feature("x.other", "producer", NOW, 2))
    assert store.count == 1


def test_manifest_rejects_overlap_duplicate_invalid_api_and_nan_parameters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        ModuleManifest(name=" bad")
    with pytest.raises(ValueError, match="duplicates"):
        ModuleManifest(name="m", produced_features=("m.x", "m.x"))
    with pytest.raises(ValueError, match="both required and produced"):
        ModuleManifest(name="m", required_features=("m.x",), produced_features=("m.x",))
    with pytest.raises(ValueError, match="MAJOR.MINOR"):
        ModuleManifest(name="m", api_version="one")
    manifest = ModuleManifest(
        name="m",
        parameters_schema={"threshold": {"type": "number", "minimum": 0}},
    )
    with pytest.raises(ModuleConfigurationError, match="finite"):
        validate_parameters(manifest, {"threshold": math.nan})

    config = tmp_path / "config.yaml"
    config.write_text("modules: []\n" + "#" * 100, encoding="utf-8")
    with pytest.raises(ModuleConfigurationError, match="size limit"):
        ModuleRegistry(max_config_bytes=10).load_config(config)


class Stateful(StrategyModule):
    def __init__(self, name: str, fail_value: int | None = None) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name, state_schema_version="1")
        self.value = 0
        self.fail_value = fail_value

    def on_bar(self, view):
        from modular_core.module_api import ModuleDecision

        return ModuleDecision()

    def snapshot_state(self):
        return {"value": self.value}

    def restore_state(self, state):
        value = int(state["value"])
        if self.fail_value == value:
            raise ValueError("restore failure")
        self.value = value


def test_module_restore_is_atomic() -> None:
    first = Stateful("first")
    second = Stateful("second", fail_value=2)
    first.value = 10
    second.value = 20
    states = (
        ModuleState("first", "0.1.0", "1", {"value": 1}),
        ModuleState("second", "0.1.0", "1", {"value": 2}),
    )
    with pytest.raises(ValueError, match="restore failure"):
        restore_module_states((first, second), states)
    assert (first.value, second.value) == (10, 20)


def test_checkpoint_integrity_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    module = Stateful("stateful")
    checkpoint = build_checkpoint(
        run_id="run",
        kernel_version="0.4.1",
        created_at=NOW,
        last_bar_index=0,
        last_bar_time=NOW,
        data_hash="data",
        config_hash="config",
        module_hash="module",
        modules=(module,),
    )
    assert checkpoint.integrity_sha256 is not None
    repository = AtomicJsonStateRepository(tmp_path)
    manager = CheckpointManager(repository)
    manager.save("checkpoint", checkpoint)
    assert manager.load("checkpoint") == checkpoint

    path = tmp_path / "checkpoint.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["data_hash"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        manager.load("checkpoint")


def test_set_serialization_is_deterministic() -> None:
    assert to_jsonable({"z", "a", "m"}) == ["a", "m", "z"]
