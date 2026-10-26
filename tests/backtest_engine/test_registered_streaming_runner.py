from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

pytest.importorskip("market_data_ingestion")

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.timeframes import timeframe_delta
from modular_backtest import runner
from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.provenance import SourceProvenance
from modular_backtest.scalability import PerformanceConfig
from modular_backtest.streaming_data import PreparedCandleSource

_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_BATCH_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"


class _FakeResult:
    journal = SimpleNamespace(trades=[], fills=[], equity=[], errors=[])
    run_manifest = SimpleNamespace(run_id="registered-streaming-run")
    bars_processed = 17

    def export_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "run.json").write_text(
            json.dumps({"manifest": {"run_id": self.run_manifest.run_id}}),
            encoding="utf-8",
        )
        (path / "metrics.json").write_text("{}", encoding="utf-8")
        return path


class _FakeCheckpoint:
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "test",
            "recovery_mode": "bounded_deterministic_prefix_replay",
            "source_position": {"partition_index": 1, "row_offset": 2},
        }


class _FakeReplay:
    result = _FakeResult()

    def __init__(self, *, restarted: bool) -> None:
        self.checkpoint = _FakeCheckpoint() if restarted else None
        self.restart_verified = restarted
        self.continuous_sha256 = "streaming-result"
        self.restarted_sha256 = "streaming-result" if restarted else None

    @property
    def recovery_mode(self) -> str:
        return "bounded_deterministic_prefix_replay" if self.restart_verified else "disabled"

    @property
    def simulation_passes(self) -> int:
        return 2 if self.restart_verified else 1

    @property
    def source_traversals(self) -> int:
        return 2 if self.restart_verified else 1

    @property
    def source_rows_replayed(self) -> int:
        return 0

    def evidence(self) -> dict[str, object]:
        return {
            "restart_verified": self.restart_verified,
            "recovery_mode": self.recovery_mode,
            "simulation_passes": self.simulation_passes,
            "source_traversals": self.source_traversals,
            "source_rows_replayed": self.source_rows_replayed,
            "continuous_sha256": self.continuous_sha256,
            "restarted_sha256": self.restarted_sha256,
            "checkpoint": (self.checkpoint.to_dict() if self.checkpoint is not None else None),
        }


def _provenance() -> SourceProvenance:
    return SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.2",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )


def _registered(tmp_path: Path, *, rows: int = 17) -> DataSourceConfig:
    source = tmp_path / "BTCUSDT_5m.parquet"
    step = timeframe_delta("5m")
    start = datetime(2024, 1, 31, 23, 30, tzinfo=UTC)
    open_times = [start + index * step for index in range(rows)]
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [100.0 + index for index in range(rows)],
                "high": [101.0 + index for index in range(rows)],
                "low": [99.0 + index for index in range(rows)],
                "close": [100.5 + index for index in range(rows)],
                "volume": [10.0 + index for index in range(rows)],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1000.0 + index for index in range(rows)],
                "trades": [10 + index for index in range(rows)],
                "taker_buy_base": [5.0 + index for index in range(rows)],
                "taker_buy_quote": [500.0 + index for index in range(rows)],
            }
        ),
        source,
        compression="zstd",
    )
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(
            ExternalCorpusFile(
                symbol="BTCUSDT",
                timeframe="5m",
                path=source,
                source_uri="local-corpus://registered-streaming-runner/btcusdt/5m",
                expected_rows=rows,
                expected_first_open_time=open_times[0],
                expected_last_open_time=open_times[-1],
                expected_sha256=sha256_file(source),
            ),
        ),
    )
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    registered = ExternalCorpusRegistrar(storage=storage, registry=registry).register(spec)
    descriptor = registered.datasets[0].descriptor
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage.root,
        dataset_id=descriptor.dataset_id,
        dataset_version=descriptor.version,
        registered_mode="streaming",
        stream_batch_size=2,
    )


def test_runner_uses_streaming_source_and_publishes_loader_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _registered(tmp_path)
    config = BacktestAppConfig(
        data=data,
        modules=tmp_path / "unused-modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        seed=11,
    )
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)
    captured: dict[str, object] = {}

    def fake_run_single_pass(
        selected_config: BacktestAppConfig,
        source: PreparedCandleSource,
        performance: object,
        funding_source: object | None = None,
    ) -> _FakeReplay:
        assert selected_config is config
        assert performance is not None
        assert funding_source is None
        captured["source_mode"] = source.source_mode
        captured["stream_batch_size"] = source.stream_batch_size
        captured["candles"] = tuple(source.iter_candles())
        return _FakeReplay(restarted=False)

    monkeypatch.setattr(runner, "_run_single_pass", fake_run_single_pass)

    outcome = runner.run_backtest(config)

    assert captured["source_mode"] == "streaming"
    assert captured["stream_batch_size"] == 2
    assert len(captured["candles"]) == 17
    assert outcome.validation.to_dict()["source_mode"] == "streaming"
    assert outcome.validation.to_dict()["stream_batch_size"] == 2

    data_validation = json.loads(
        (outcome.output_directory / "data-validation.json").read_text(encoding="utf-8")
    )
    application = json.loads(
        (outcome.output_directory / "application-run.json").read_text(encoding="utf-8")
    )
    assert data_validation["source_mode"] == "streaming"
    assert data_validation["stream_batch_size"] == 2
    assert application["validation"]["source_mode"] == "streaming"
    assert application["validation"]["stream_batch_size"] == 2
    assert application["checkpoint_mode"] == "disabled"
    assert application["recovery_mode"] == "disabled"
    assert application["simulation_passes"] == 1
    assert application["source_traversals"] == 1
    assert application["source_rows_replayed"] == 0
    assert application["restart_verified"] is False


def test_runner_routes_registered_streaming_replay_and_publishes_cursor_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _registered(tmp_path)
    config = BacktestAppConfig(
        data=data,
        modules=tmp_path / "unused-modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        seed=19,
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=7),
    )
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)
    captured: dict[str, Any] = {}

    def fake_registered_replay(
        engine_factory: object,
        source: PreparedCandleSource,
        *,
        restart_at_index: int,
        seed: int,
        commit_sha: str | None,
    ) -> _FakeReplay:
        captured["engine_factory"] = engine_factory
        captured["source_mode"] = source.source_mode
        captured["stream_batch_size"] = source.stream_batch_size
        captured["restart_at_index"] = restart_at_index
        captured["seed"] = seed
        captured["commit_sha"] = commit_sha
        return _FakeReplay(restarted=True)

    monkeypatch.setattr(
        runner,
        "run_with_registered_streaming_replay_restart",
        fake_registered_replay,
    )
    monkeypatch.setattr(
        runner,
        "run_with_replay_restart",
        lambda *args, **kwargs: pytest.fail(f"legacy replay selected: {args!r} {kwargs!r}"),
    )

    outcome = runner.run_backtest(
        config,
        PerformanceConfig(mode="canonical_audit", max_memory_mb=None),
    )

    assert captured["source_mode"] == "streaming"
    assert captured["stream_batch_size"] == 2
    assert captured["restart_at_index"] == 7
    assert captured["seed"] == 19
    assert captured["commit_sha"] == "d" * 40
    assert callable(captured["engine_factory"])
    assert outcome.restart_verified is True

    application = json.loads(
        (outcome.output_directory / "application-run.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (outcome.output_directory / "restart-evidence.json").read_text(encoding="utf-8")
    )
    checkpoint = json.loads(
        (outcome.output_directory / "replay-checkpoint.json").read_text(encoding="utf-8")
    )
    assert application["checkpoint_mode"] == "replay"
    assert application["recovery_mode"] == "bounded_deterministic_prefix_replay"
    assert application["simulation_passes"] == 2
    assert application["source_traversals"] == 2
    assert application["source_rows_replayed"] == 0
    assert application["restart_verified"] is True
    assert application["validation"]["source_mode"] == "streaming"
    assert evidence["restart_verified"] is True
    assert checkpoint["recovery_mode"] == "bounded_deterministic_prefix_replay"
    assert checkpoint["source_position"] == {
        "partition_index": 1,
        "row_offset": 2,
    }


def test_runner_rejects_registered_streaming_replay_in_research_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BacktestAppConfig(
        data=_registered(tmp_path),
        modules=tmp_path / "unused-modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=7),
    )
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)

    with pytest.raises(
        ValueError,
        match="requires performance mode canonical_audit",
    ):
        runner.run_backtest(
            config,
            PerformanceConfig(mode="research_fast", max_memory_mb=None),
        )


def test_runner_environment_can_roll_back_checkpoint_route_to_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BacktestAppConfig(
        data=_registered(tmp_path),
        modules=tmp_path / "unused-modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=7),
    )
    monkeypatch.setenv(_MODE_ENV, "legacy_materialized")
    monkeypatch.delenv(_BATCH_ENV, raising=False)

    assert runner._registered_checkpoint_uses_streaming(config) is False


def test_runner_rejects_batch_override_for_legacy_checkpoint_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = BacktestAppConfig(
        data=_registered(tmp_path),
        modules=tmp_path / "unused-modules.yaml",
        output=OutputConfig(tmp_path / "results"),
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=7),
    )
    monkeypatch.setenv(_MODE_ENV, "legacy_materialized")
    monkeypatch.setenv(_BATCH_ENV, "64")

    with pytest.raises(ValueError, match="batch size requires"):
        runner._registered_checkpoint_uses_streaming(config)
