from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import runner
from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.data import ValidationSummary
from modular_backtest.provenance import SourceProvenance
from modular_backtest.scalability import PerformanceConfig, ScalableJournal, StreamingEventStore


def _config(tmp_path: Path) -> BacktestAppConfig:
    return BacktestAppConfig(
        data=DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT"),
        modules=tmp_path / "modules.yaml",
        requested_timeframes=("15m", "1h", "4h", "1d"),
        output=OutputConfig(tmp_path / "results"),
        seed=7,
        checkpoint=CheckpointConfig(),
    )


def _validation(config: BacktestAppConfig) -> ValidationSummary:
    return ValidationSummary(
        path=str(config.data.path),
        sha256="abc",
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=1,
        first_timestamp="2024-01-01T00:00:00+00:00",
        last_timestamp="2024-01-01T00:00:00+00:00",
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
    )


def _provenance() -> SourceProvenance:
    return SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.0",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )


class _Replay:
    checkpoint = None
    restart_verified = False
    continuous_sha256 = None
    restarted_sha256 = None
    recovery_mode = "disabled"
    simulation_passes = 1
    source_traversals = 1
    source_rows_replayed = 0

    def evidence(self) -> dict[str, object]:
        return {"restart_verified": False, "checkpoint": None}


class _Result:
    def __init__(self, journal: ScalableJournal) -> None:
        self.journal = journal
        self.run_manifest = SimpleNamespace(run_id="run-schema")
        self.bars_processed = 1

    def export_directory(self, path: Path) -> Path:
        self.journal.export_directory(path)
        (path / "run.json").write_text(
            json.dumps({"manifest": {"run_id": "run-schema"}}),
            encoding="utf-8",
        )
        (path / "metrics.json").write_text("{}", encoding="utf-8")
        return path


@pytest.mark.parametrize(
    ("mode", "expected_schema", "expected_encoding"),
    (("canonical_audit", "2", "identity"), ("research_fast", "3", "gzip")),
)
def test_application_schema_matches_scalable_journal_representation(
    tmp_path: Path,
    mode: str,
    expected_schema: str,
    expected_encoding: str,
) -> None:
    config = _config(tmp_path)
    performance = PerformanceConfig(mode=mode)
    store = StreamingEventStore(tmp_path / f"{mode}.events")
    journal = ScalableJournal(
        run_id="run-schema",
        event_store=store,
        performance=performance,
    )
    journal.emit_event(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="test.event",
        source="test",
        payload={"value": 1},
    )
    output = tmp_path / f"result-{mode}"

    runner._write_result_files(
        output,
        result=_Result(journal),
        replay=_Replay(),
        validation=_validation(config),
        config=config,
        provenance=_provenance(),
        performance=performance,
        application_id="run-schema",
        funding_source=None,
    )

    application = json.loads((output / "application-run.json").read_text(encoding="utf-8"))
    journal_payload = json.loads((output / "journal.json").read_text(encoding="utf-8"))
    environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))

    assert application["artifact_schema_version"] == expected_schema
    assert journal_payload["schema_version"] == expected_schema
    assert journal_payload["event_stream"]["encoding"] == expected_encoding
    assert environment["artifact_schema_version"] == expected_schema
