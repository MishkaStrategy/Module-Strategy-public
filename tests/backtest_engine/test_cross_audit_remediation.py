from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import runner
from modular_backtest.config import (
    BacktestAppConfig,
    ConfigurationError,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.data import ValidationSummary
from modular_backtest.metrics import calculate_extended_metrics
from modular_backtest.provenance import SourceProvenance
from modular_backtest.results import ResultError, inspect_result, write_artifact_manifest


def _config(tmp_path: Path, **instrument: object) -> BacktestAppConfig:
    return BacktestAppConfig(
        data=DataSourceConfig(
            path=tmp_path / "bars.csv",
            format="csv",
            symbol="BTCUSDT",
        ),
        modules=tmp_path / "modules.yaml",
        instrument=instrument,
        output=OutputConfig(tmp_path / "results"),
    )


def test_binance_usdt_m_rejects_incompatible_instrument_market_type(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, market_type="spot")

    with pytest.raises(ConfigurationError, match="requires instrument.market_type"):
        config.instrument_spec()

    assert _config(tmp_path).instrument_spec().market_type == "linear_futures"


def test_non_unit_contract_size_is_included_in_total_slippage() -> None:
    metrics = calculate_extended_metrics(
        trades=[],
        fills=[SimpleNamespace(fee=0.0, slippage=0.5, quantity=2.0)],
        equity=[],
        initial_equity=1_000.0,
        contract_size=10.0,
    )

    assert metrics["total_slippage"] == 10.0
    with pytest.raises(ValueError, match="finite positive"):
        calculate_extended_metrics(
            trades=[],
            fills=[],
            equity=[],
            initial_equity=1_000.0,
            contract_size=float("nan"),
        )


def test_module_finalization_failure_prevents_result_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    validation = ValidationSummary(
        path=str(config.data.path),
        sha256="a" * 64,
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
    provenance = SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.0",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )
    result = SimpleNamespace(
        journal=SimpleNamespace(
            errors=[
                {
                    "stage": "module_finish:broken",
                    "error_type": "RuntimeError",
                    "error_message": "boom",
                }
            ]
        ),
        run_manifest=SimpleNamespace(run_id="must-not-publish"),
        bars_processed=1,
    )
    replay = SimpleNamespace(result=result)

    monkeypatch.setattr(runner, "build_source_provenance", lambda: provenance)
    source = SimpleNamespace(validation=validation)
    monkeypatch.setattr(runner, "prepare_candle_source", lambda data: source)
    monkeypatch.setattr(runner, "_run_single_pass", lambda *args, **kwargs: replay)

    with pytest.raises(runner.ResultPublicationError, match="module finalization failed"):
        runner.run_backtest(config)

    assert not config.output.directory.exists()


def test_non_finite_json_is_rejected_before_and_after_publication(
    tmp_path: Path,
) -> None:
    with pytest.raises(runner.ResultPublicationError, match="canonical finite JSON"):
        runner._json_text({"value": float("nan")})

    directory = tmp_path / "result"
    directory.mkdir()
    (directory / "run.json").write_text(
        '{"manifest":{"run_id":"run-nonfinite"},"bad":NaN}',
        encoding="utf-8",
    )
    (directory / "metrics.json").write_text("{}", encoding="utf-8")
    write_artifact_manifest(directory, "run-nonfinite")

    with pytest.raises(ResultError, match="non-finite"):
        inspect_result(directory)


def test_publication_syncs_tree_before_rename_and_parent_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "destination"
    staging.mkdir()
    (staging / "payload.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    write_artifact_manifest(staging, "run-sync")

    events: list[tuple[str, Path]] = []
    original_rename = Path.rename

    def record_tree(path: Path) -> None:
        events.append(("tree", path))

    def record_rename(path: Path, target: Path) -> Path:
        events.append(("rename", target))
        return original_rename(path, target)

    def record_directory(path: Path) -> None:
        events.append(("directory", path))

    monkeypatch.setattr(runner, "_fsync_tree", record_tree)
    monkeypatch.setattr(Path, "rename", record_rename)
    monkeypatch.setattr(runner, "_fsync_directory", record_directory)

    assert runner._publish_result_directory(staging, destination) == destination
    assert events == [
        ("tree", staging),
        ("rename", destination),
        ("directory", destination.parent),
    ]
