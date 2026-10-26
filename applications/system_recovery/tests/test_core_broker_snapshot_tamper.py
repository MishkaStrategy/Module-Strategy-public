from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from modular_core import (
    AtomicJsonStateRepository,
    EngineConfig,
    Journal,
    OrderRequest,
    OrderType,
    Side,
)
from modular_core.broker_state import save_broker_state
from modular_core.execution import BrokerSimulator

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "5" * 40
_RELEASE_ID = "core-broker-state-tamper"
_LOGICAL_ID = "core.paper.broker-state"
_STATE_KEY = "paper-broker"


def test_tampered_core_broker_snapshot_is_rejected_before_clean_host_publication(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    clean = tmp_path / "clean"
    source.mkdir()
    backup.mkdir()
    clean.mkdir()

    broker = BrokerSimulator(EngineConfig(), Journal())
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=1.0,
            price=90.0,
            client_order_id="tamper-proof-order",
        ),
        source_module="recovery-test",
        created_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None

    repository = AtomicJsonStateRepository(source / "core" / "broker")
    save_broker_state(repository, _STATE_KEY, broker)
    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id=_LOGICAL_ID,
            owner="modular-core",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=f"core/broker/{_STATE_KEY}.json",
            restore_target=f"core/broker/{_STATE_KEY}.json",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(entry,),
    )

    payload = backup / "payload" / _LOGICAL_ID
    payload.write_bytes(payload.read_bytes() + b"\n")

    with pytest.raises(RecoveryManifestError, match="byte size differs"):
        restore_snapshot_entry(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            logical_id=_LOGICAL_ID,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )

    assert not (clean / "core" / "broker" / f"{_STATE_KEY}.json").exists()
