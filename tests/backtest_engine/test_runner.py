from __future__ import annotations

import json
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
from modular_backtest.results import verify_artifact_manifest, write_artifact_manifest


def _config(
    tmp_path: Path,
    *,
    checkpoint: CheckpointConfig | None = None,
) -> BacktestAppConfig:
    return BacktestAppConfig(
        data=DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT"),
        modules=tmp_path / "modules.yaml",
        requested_timeframes=("15m", "1h", "4h", "1d"),
        output=OutputConfig(tmp_path / "results"),
        seed=7,
        checkpoint=checkpoint or CheckpointConfig(),
    )


def test_timeframe_specs_are_utc_aligned() -> None:
    specs = runner._timeframe_specs(("15m", "1h", "4h", "1d"))
    assert [spec.ratio for spec in specs] == [3, 12, 48, 288]
    assert all(spec.align_to_clock and spec.gap_safe for spec in specs)
    assert all(spec.alignment_timezone == "UTC" for spec in specs)


def test_restart_index_defaults_to_midpoint_and_validates_boundaries(tmp_path: Path) -> None:
    replay_config = _config(tmp_path, checkpoint=CheckpointConfig(mode="replay"))
    assert runner._restart_index(replay_config, 10) == 4
    assert runner._restart_index(_config(tmp_path), 10) is None

    with pytest.raises(ValueError, match="at least two"):
        runner._restart_index(replay_config, 1)

    explicit = _config(
        tmp_path,
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=9),
    )
    with pytest.raises(ValueError, match="leave a candle"):
        runner._restart_index(explicit, 10)


def test_run_backtest_exports_checksummed_application_and_restart_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path,
        checkpoint=CheckpointConfig(mode="replay", restart_at_bar=0),
    )
    validation = ValidationSummary(
        path=str(config.data.path),
        sha256="abc",
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=2,
        first_timestamp="2024-01-01T00:00:00+00:00",
        last_timestamp="2024-01-01T00:05:00+00:00",
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
    )
    provenance = SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.0",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )
    monkeypatch.setattr(runner, "build_source_provenance", lambda: provenance)
    source = SimpleNamespace(total_bars=2, validation=validation)
    monkeypatch.setattr(runner, "prepare_candle_source", lambda data: source)

    class FakeRegistry:
        def load_config(self, path: Path) -> tuple[str, ...]:
            return ("module",)

    captured: dict[str, object] = {}

    class FakeResult:
        journal = SimpleNamespace(trades=[], fills=[], equity=[])
        run_manifest = SimpleNamespace(run_id="run-test")
        bars_processed = 2

        def export_directory(self, path: Path) -> Path:
            path.mkdir(parents=True, exist_ok=True)
            (path / "run.json").write_text(
                json.dumps({"manifest": {"run_id": "run-test"}}),
                encoding="utf-8",
            )
            (path / "metrics.json").write_text("{}", encoding="utf-8")
            return path

    class FakeEngine:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeReplay:
        result = FakeResult()
        checkpoint = None
        restart_verified = True
        continuous_sha256 = None
        restarted_sha256 = "same"
        recovery_mode = "runtime_snapshot_v1"
        simulation_passes = 1
        source_traversals = 2
        source_rows_replayed = 0

        def evidence(self) -> dict[str, object]:
            return {
                "restart_verified": True,
                "recovery_mode": "runtime_snapshot_v1",
                "simulation_passes": 1,
                "source_traversals": 2,
                "source_rows_replayed": 0,
                "continuous_sha256": None,
                "restarted_sha256": "same",
                "checkpoint": None,
            }

    def fake_run(
        engine_factory: object,
        prepared_source: object,
        **kwargs: object,
    ) -> FakeReplay:
        assert callable(engine_factory)
        engine_factory()
        assert prepared_source is source
        assert kwargs["restart_at_index"] == 0
        assert kwargs["seed"] == 7
        assert kwargs["commit_sha"] == "d" * 40
        return FakeReplay()

    monkeypatch.setattr(runner, "ModuleRegistry", FakeRegistry)
    monkeypatch.setattr(runner, "ScalableBacktestEngine", FakeEngine)
    monkeypatch.setattr(runner, "run_with_runtime_snapshot_restart", fake_run)

    outcome = runner.run_backtest(config)
    repeated = runner.run_backtest(config)

    assert repeated.output_directory == outcome.output_directory
    assert outcome.run_id == "run-test"
    assert outcome.bars_processed == 2
    assert outcome.restart_verified is True
    assert captured["modules"] == ("module",)
    assert not list(config.output.directory.glob(".*.staging-*"))

    application = json.loads((outcome.output_directory / "application-run.json").read_text())
    assert application["application_version"] == "0.1.0"
    assert application["funding_model"] == "disabled"
    assert application["seed"] == 7
    assert application["checkpoint_mode"] == "replay"
    assert application["recovery_mode"] == "runtime_snapshot_v1"
    assert application["restart_verified"] is True
    assert application["simulation_passes"] == 1
    assert application["source_traversals"] == 2
    assert application["source_rows_replayed"] == 0
    assert application["snapshot_schema_version"] is None
    assert application["snapshot_bytes"] is None
    assert application["snapshot_event_stream_bytes"] is None
    assert application["source_provenance"]["source_commit_sha"] == "d" * 40
    assert application["source_provenance"]["application_source_sha256"] == "a" * 64
    assert application["source_provenance"]["core_source_sha256"] == "b" * 64
    data_validation = json.loads((outcome.output_directory / "data-validation.json").read_text())
    assert data_validation["sha256"] == "abc"
    extended = json.loads((outcome.output_directory / "metrics-extended.json").read_text())
    assert extended["trades"] == 0
    recovery = json.loads((outcome.output_directory / "restart-evidence.json").read_text())
    assert recovery["continuous_sha256"] is None
    assert recovery["restarted_sha256"] == "same"
    assert recovery["simulation_passes"] == 1
    manifest = verify_artifact_manifest(outcome.output_directory)
    assert manifest["run_id"] == "run-test"
    assert "application-run.json" in manifest["files"]


def _artifact_directory(path: Path, run_id: str, payload: str) -> Path:
    path.mkdir()
    (path / "payload.txt").write_text(payload, encoding="utf-8")
    write_artifact_manifest(path, run_id)
    return path


def test_publish_reuses_identical_result_and_removes_staging(tmp_path: Path) -> None:
    destination = _artifact_directory(tmp_path / "destination", "run-one", "same")
    staging = _artifact_directory(tmp_path / "staging", "run-one", "same")

    published = runner._publish_result_directory(staging, destination)

    assert published == destination
    assert destination.is_dir()
    assert not staging.exists()


def test_publish_rejects_conflicting_or_corrupted_existing_result(tmp_path: Path) -> None:
    destination = _artifact_directory(tmp_path / "destination", "run-one", "original")
    conflicting = _artifact_directory(tmp_path / "conflicting", "run-one", "different")
    with pytest.raises(runner.ResultPublicationError, match="conflicts"):
        runner._publish_result_directory(conflicting, destination)
    assert conflicting.exists()

    (destination / "payload.txt").write_text("tampered", encoding="utf-8")
    staging = _artifact_directory(tmp_path / "staging", "run-one", "original")
    with pytest.raises(runner.ResultPublicationError, match="corrupted"):
        runner._publish_result_directory(staging, destination)
    assert staging.exists()
