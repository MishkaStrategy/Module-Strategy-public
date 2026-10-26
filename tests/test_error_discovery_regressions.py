from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest

from modular_core.checkpoints import (
    CheckpointManager,
    KernelCheckpoint,
    ModuleState,
    restore_module_states,
)
from modular_core.extensions import (
    AtomicJsonStateRepository,
    ExecutionStateSnapshot,
    ReconciliationReport,
    reconcile_execution_state,
)
from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule

UTC = timezone.utc
NOW = datetime(2026, 7, 21, tzinfo=UTC)


class StatefulProbe(StrategyModule):
    def __init__(self, name: str, *, mutate_then_fail: bool = False) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name, version="1.0.0", state_schema_version="1")
        self.value = 0
        self.mutate_then_fail = mutate_then_fail

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"value": self.value}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.value = int(state["value"])
        if self.mutate_then_fail:
            raise RuntimeError(f"{self.name} restore failed after mutation")


def test_atomic_restore_rolls_back_module_that_mutates_then_raises() -> None:
    first = StatefulProbe("first")
    second = StatefulProbe("second", mutate_then_fail=True)
    first.value = 10
    second.value = 20
    states = (
        ModuleState("first", "1.0.0", "1", {"value": 1}),
        ModuleState("second", "1.0.0", "1", {"value": 2}),
    )

    with pytest.raises(RuntimeError, match="failed after mutation"):
        restore_module_states((first, second), states)

    assert first.value == 10
    assert second.value == 20


def test_atomic_json_repository_load_is_deeply_immutable(tmp_path: Path) -> None:
    repository = AtomicJsonStateRepository(tmp_path)
    repository.save("state", {"nested": {"values": [1, 2]}})

    loaded = repository.load("state")
    assert loaded is not None
    with pytest.raises(TypeError):
        loaded["nested"]["values"] += (3,)  # type: ignore[index,operator]


def test_checkpoint_rejects_non_hex_integrity_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256 hex digest"):
        KernelCheckpoint(
            checkpoint_id="checkpoint-x",
            run_id="run-x",
            kernel_version="0.4.2",
            created_at=NOW,
            last_bar_index=0,
            last_bar_time=NOW,
            data_hash="data",
            config_hash="config",
            module_hash="module",
            module_states=(),
            integrity_sha256="z" * 64,
        )


def test_checkpoint_manager_rejects_unsupported_schema(tmp_path: Path) -> None:
    repository = AtomicJsonStateRepository(tmp_path)
    repository.save(
        "checkpoint",
        {
            "checkpoint_id": "checkpoint-x",
            "run_id": "run-x",
            "kernel_version": "0.4.2",
            "created_at": NOW.isoformat(),
            "last_bar_index": 0,
            "last_bar_time": NOW.isoformat(),
            "data_hash": "data",
            "config_hash": "config",
            "module_hash": "module",
            "module_states": [],
            "schema_version": "999",
        },
    )

    with pytest.raises(ValueError, match="unsupported checkpoint schema"):
        CheckpointManager(repository).load("checkpoint")


def test_reconciliation_detects_client_order_identity_mismatch() -> None:
    local = ExecutionStateSnapshot(
        NOW,
        pending_order_ids=("order-1",),
        client_order_ids=("client-local",),
    )
    remote = ExecutionStateSnapshot(
        NOW,
        pending_order_ids=("order-1",),
        client_order_ids=("client-remote",),
    )

    report = reconcile_execution_state(local, remote)

    assert not report.matched
    assert report.safe_halt_required
    assert report.missing_local_client_order_ids == ("client-remote",)
    assert report.missing_remote_client_order_ids == ("client-local",)


def test_execution_snapshot_rejects_blank_identity() -> None:
    with pytest.raises(ValueError, match="pending order id"):
        ExecutionStateSnapshot(NOW, pending_order_ids=(" ",))
    with pytest.raises(ValueError, match="client order id"):
        ExecutionStateSnapshot(NOW, client_order_ids=("",))


def test_reconciliation_client_identity_is_symmetric_and_matching_state_passes() -> None:
    left = ExecutionStateSnapshot(
        NOW,
        pending_order_ids=("order-1", "order-2"),
        client_order_ids=("client-1", "client-2"),
    )
    right = ExecutionStateSnapshot(
        NOW,
        pending_order_ids=("order-2", "order-1"),
        client_order_ids=("client-2", "client-1"),
    )
    report = reconcile_execution_state(left, right)
    assert report.matched
    assert not report.safe_halt_required


def test_checkpoint_manager_allows_explicit_supported_schema(tmp_path: Path) -> None:
    repository = AtomicJsonStateRepository(tmp_path)
    repository.save(
        "checkpoint",
        {
            "checkpoint_id": "checkpoint-v2",
            "run_id": "run-v2",
            "kernel_version": "0.4.2",
            "created_at": NOW.isoformat(),
            "last_bar_index": 0,
            "last_bar_time": NOW.isoformat(),
            "data_hash": "data",
            "config_hash": "config",
            "module_hash": "module",
            "module_states": [],
            "schema_version": "2",
        },
    )
    loaded = CheckpointManager(repository, supported_schema_versions=("1", "2")).load("checkpoint")
    assert loaded is not None
    assert loaded.schema_version == "2"


def test_execution_snapshot_canonicalizes_mutable_identity_sequences() -> None:
    pending = ["order-1"]
    clients = ["client-1"]
    snapshot = ExecutionStateSnapshot(
        NOW,
        pending_order_ids=pending,  # type: ignore[arg-type]
        client_order_ids=clients,  # type: ignore[arg-type]
    )
    pending.append("order-2")
    clients.append("client-2")
    assert snapshot.pending_order_ids == ("order-1",)
    assert snapshot.client_order_ids == ("client-1",)


def test_reconciliation_report_canonicalizes_mutable_identity_sequences() -> None:
    missing = ["order-1"]
    report = ReconciliationReport(
        matched=False,
        missing_local_order_ids=missing,  # type: ignore[arg-type]
        safe_halt_required=True,
    )
    missing.append("order-2")
    assert report.missing_local_order_ids == ("order-1",)
    assert report.issue_count == 1


def test_execution_snapshot_rejects_non_string_identity() -> None:
    with pytest.raises(TypeError, match="pending order id must be str"):
        ExecutionStateSnapshot(NOW, pending_order_ids=[1])  # type: ignore[list-item,arg-type]


def test_checkpoint_rejects_noncanonical_uppercase_integrity_digest() -> None:
    with pytest.raises(ValueError, match="canonical lowercase"):
        KernelCheckpoint(
            checkpoint_id="checkpoint-uppercase",
            run_id="run-uppercase",
            kernel_version="0.4.2",
            created_at=NOW,
            last_bar_index=0,
            last_bar_time=NOW,
            data_hash="data",
            config_hash="config",
            module_hash="module",
            module_states=(),
            integrity_sha256="A" * 64,
        )
