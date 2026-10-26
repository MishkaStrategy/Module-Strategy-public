from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Mapping

import pytest

from modular_core.checkpoints import (
    CheckpointManager,
    KernelCheckpoint,
    build_checkpoint,
)
from modular_core.extensions import InMemoryStateRepository
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.run_manifest import RunManifest, build_run_manifest
from modular_core.serialization import to_jsonable


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SnapshotProbe(StrategyModule):
    name = "snapshot-probe"
    manifest = ModuleManifest(
        name=name,
        version="1.0.0",
        state_schema_version="1",
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"value": 1}


def _checkpoint() -> KernelCheckpoint:
    return build_checkpoint(
        run_id="run-1",
        kernel_version="0.4.2",
        created_at=NOW + timedelta(minutes=1),
        last_bar_index=10,
        last_bar_time=NOW,
        data_hash="data-v1",
        config_hash="config-v1",
        module_hash="modules-v1",
        modules=(SnapshotProbe(),),
    )


def _bar(index: int) -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _run_manifest() -> RunManifest:
    return build_run_manifest(
        kernel_version="0.4.2",
        created_at=NOW,
        bars=(_bar(0), _bar(1)),
        config={"risk": 0.005},
        manifests=(ModuleManifest(name="probe", version="1.0.0"),),
        module_parameters=({"window": 20},),
    )


def _run_id_for_hashes(
    kernel_version: str,
    data_hash: str,
    config_hash: str,
    module_hash: str,
) -> str:
    encoded = json.dumps(
        (kernel_version, data_hash, config_hash, module_hash),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "run-" + hashlib.sha256(encoded).hexdigest()[:16]


def test_checkpoint_manager_rejects_unsigned_checkpoint_before_persisting() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    unsigned = replace(_checkpoint(), integrity_sha256=None)

    with pytest.raises(
        ValueError,
        match="checkpoint integrity_sha256 is required for persistence",
    ):
        manager.save("checkpoint", unsigned)

    assert repository.load("checkpoint") is None


def test_checkpoint_manager_loads_legacy_unsigned_checkpoint() -> None:
    repository = InMemoryStateRepository()
    manager = CheckpointManager(repository)
    unsigned = replace(_checkpoint(), integrity_sha256=None)
    payload = to_jsonable(unsigned)
    assert isinstance(payload, dict)
    repository.save("checkpoint", payload)

    assert manager.load("checkpoint") == unsigned


def test_run_manifest_rejects_forged_module_hash_with_matching_run_id() -> None:
    manifest = _run_manifest()
    forged_module_hash = "0" * 64
    forged_run_id = _run_id_for_hashes(
        manifest.kernel_version,
        manifest.data_hash,
        manifest.config_hash,
        forged_module_hash,
    )

    with pytest.raises(
        ValueError,
        match="module_hash is inconsistent with manifests and parameters",
    ):
        replace(
            manifest,
            module_hash=forged_module_hash,
            run_id=forged_run_id,
        )
