from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from modular_core.events import InMemoryEventStore

from paper_execution import PaperExecutionConfig, PaperExecutionCoordinator
from paper_execution._utils import canonical_json, sha256_text
from paper_execution.manifest import MODULE_VERSION
from paper_execution.orchestration_state import ExecutionOrchestrationState
from paper_execution.recovery import restore_execution_state
from paper_execution.recovery_types import STATE_SCHEMA_VERSION


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _coordinator(broker, metadata, run_id):
    store = InMemoryEventStore()
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )
    return coordinator, store


def _accept(coordinator, plan, versions, now):
    return coordinator.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )


def _source(plan, versions, now, broker, metadata, run_id):
    coordinator, store = _coordinator(broker, metadata, run_id)
    result = _accept(coordinator, plan, versions, now)
    assert result.accepted
    return coordinator, store, coordinator.snapshot_state()


def _rehash(state: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def _decode(source, state):
    return restore_execution_state(
        state,
        expected_configuration_hash=source.config.canonical_hash,
    )


def test_source_accepts_plan(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _ = _coordinator(broker, metadata, "accept-source")
    result = _accept(source, plan, versions, now)
    assert result.accepted, (result.reason_code, result.message)
    assert result.client_order_ids
    assert source.orders


def test_accepted_source_creates_snapshot(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _ = _coordinator(broker, metadata, "snapshot-source")
    result = _accept(source, plan, versions, now)
    assert result.accepted, (result.reason_code, result.message)
    state = source.snapshot_state()
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert state["configuration_hash"] == source.config.canonical_hash
    assert state["plans"]
    assert state["orders"]


def test_minimal_manual_schema3_root_decodes() -> None:
    config = PaperExecutionConfig()
    body: dict[str, object] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "module_version": MODULE_VERSION,
        "configuration_hash": config.canonical_hash,
        "plans": [],
        "orders": [],
        "blocked_scopes": [],
        "event_sequence": 0,
        "journal_attestation": {},
        "orchestration_state": ExecutionOrchestrationState().snapshot_state(),
    }
    state = {**body, "state_hash": sha256_text(canonical_json(body))}
    restored = restore_execution_state(
        state,
        expected_configuration_hash=config.canonical_hash,
    )
    assert restored.plan_results == {}
    assert restored.orders == {}
    assert restored.source_schema_version == STATE_SCHEMA_VERSION


def test_fresh_empty_envelope_decodes(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _, snapshot = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "codec-empty",
    )
    state = deepcopy(dict(snapshot))
    state["plans"] = []
    state["orders"] = []
    state["blocked_scopes"] = []
    state["event_sequence"] = 0
    restored = _decode(source, _rehash(state))
    assert restored.plan_results == {}
    assert restored.orders == {}


def test_fresh_plan_rows_decode_without_order_references(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _, snapshot = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "codec-plans",
    )
    state = deepcopy(dict(snapshot))
    state["orders"] = []
    for row in state["plans"]:
        row["client_order_ids"] = []
    restored = _decode(source, _rehash(state))
    assert set(restored.plan_results) == set(source._plan_results)
    assert restored.orders == {}


def test_fresh_order_rows_decode_without_plans(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _, snapshot = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "codec-orders",
    )
    state = deepcopy(dict(snapshot))
    state["plans"] = []
    restored = _decode(source, _rehash(state))
    assert restored.plan_results == {}
    assert set(restored.orders) == set(source.orders)


def test_fresh_snapshot_decodes(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _, state = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "codec-decode",
    )
    restored = _decode(source, state)
    assert restored.source_schema_version == state["schema_version"]


def test_fresh_snapshot_passes_pure_recovery_codec(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, _, state = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "codec-stage",
    )
    restored = _decode(source, state)
    assert restored.plan_results == source._plan_results
    assert restored.plan_scopes == source._plan_scopes
    assert restored.orders == source.orders


def test_fresh_snapshot_journal_attestation_matches_shared_store(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, store, state = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "journal-stage",
    )
    target = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="journal-stage",
        event_store=store,
    )
    target.journal.verify_attestation(state["journal_attestation"])
    assert target.journal.attestation() == source.journal.attestation()


def test_coordinator_accepts_fresh_snapshot(
    plan,
    versions,
    now,
    broker,
    metadata,
) -> None:
    source, store, state = _source(
        plan,
        versions,
        now,
        broker,
        metadata,
        "coordinator-stage",
    )
    target = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="coordinator-stage",
        event_store=store,
    )
    target.restore_state(state)
    assert target.snapshot_state() == source.snapshot_state()
