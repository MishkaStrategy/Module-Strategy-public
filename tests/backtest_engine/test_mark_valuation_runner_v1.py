from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import runner
from modular_backtest.config import BacktestAppConfig, DataSourceConfig, OutputConfig
from modular_backtest.data import ValidationSummary
from modular_backtest.provenance import SourceProvenance
from modular_backtest.recovery import ReplayRun, _result_digest, run_with_runtime_snapshot_restart
from modular_backtest.results import inspect_result, verify_artifact_manifest
from modular_backtest.scalability import PerformanceConfig, ScalableBacktestEngine
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_backtest.valuation import (
    MARK_VALUATION_MODEL_VERSION,
    MarkValuationError,
    PriceLineageEntry,
    RegisteredMarkPriceSource,
    VALUATION_POLICY_MARK,
    valuation_execution_factory,
)
from modular_core.config import DataConfig, EngineConfig
from modular_core.instruments import InstrumentSpec
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _bars(count: int = 20) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            timestamp=START + index * STEP,
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=10.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(count)
    )


def _mark_source(
    bars: tuple[Candle, ...],
    *,
    close_offset: float = 0.0,
    dataset_hash: str = "b" * 64,
) -> RegisteredMarkPriceSource:
    end = bars[-1].timestamp + STEP
    return RegisteredMarkPriceSource(
        dataset_id="binance-btcusdt-mark",
        dataset_version=4,
        dataset_hash=dataset_hash,
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        timeframe="5m",
        window_start_inclusive=bars[0].timestamp,
        window_end_exclusive=end,
        owner_row_count=len(bars),
        owner_source_record_count=len(bars),
        lineage=(
            PriceLineageEntry(
                artifact_id="mark-runner-owner",
                source_kind="official_rest",
                checksum_sha256="a" * 64,
                record_count=len(bars),
                start=bars[0].timestamp,
                end=end,
            ),
        ),
        mark_opens=tuple(bar.open for bar in bars),
        mark_closes=tuple(bar.close + close_offset for bar in bars),
    )


class EntryModule(StrategyModule):
    name = "mark_valuation_entry_v1"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
        state_schema_version="1",
    )

    def __init__(self) -> None:
        self.sent = False
        self.counter = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.counter += 1
        if self.sent:
            return ModuleDecision()
        self.sent = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    client_order_id="mark-v1-entry",
                ),
            )
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return {"sent": self.sent, "counter": self.counter}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.sent = bool(state["sent"])
        self.counter = int(state["counter"])


class _PreparedBars:
    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self._bars = bars
        self.total_bars = len(bars)
        self.first_candle = bars[0]
        self.last_candle = bars[-1]
        self.data_hash = incremental_candle_data_hash(bars)
        self.traversals = 0

    def iter_candles(self):
        self.traversals += 1
        return iter(self._bars)

    def iter_candles_from(self, start_index: int):
        self.traversals += 1
        return iter(self._bars[start_index:])


def _performance() -> PerformanceConfig:
    return PerformanceConfig(
        mode="canonical_audit",
        max_memory_mb=1024,
        progress_interval_bars=10_000,
        max_base_history=64,
        max_timeframe_history=64,
    )


def _engine(
    mark_source: RegisteredMarkPriceSource | None = None,
) -> ScalableBacktestEngine:
    return ScalableBacktestEngine(
        EntryModule(),
        EngineConfig(
            close_open_position_at_end=True,
            data=DataConfig(
                expected_interval_seconds=300,
                gap_policy="reject",
                max_base_history=64,
            ),
        ),
        instrument=InstrumentSpec(symbol="BTCUSDT"),
        execution_factory=(
            valuation_execution_factory(mark_source) if mark_source is not None else None
        ),
        performance=_performance(),
    )


def _mark_factory(source: RegisteredMarkPriceSource):
    def create() -> ScalableBacktestEngine:
        return _engine(source)

    return create


def test_mark_equal_last_reproduces_core_result_digest_exactly() -> None:
    bars = _bars()
    baseline = _engine().run_prepared(_PreparedBars(bars))
    mark = _engine(_mark_source(bars)).run_prepared(_PreparedBars(bars))

    try:
        assert _result_digest(mark) == _result_digest(baseline)
        assert mark.journal.fills == baseline.journal.fills
        assert mark.journal.trades == baseline.journal.trades
        assert tuple(mark.journal.equity) == tuple(baseline.journal.equity)
    finally:
        baseline.journal.streaming_store.cleanup()
        mark.journal.streaming_store.cleanup()


def test_mark_not_equal_last_changes_equity_but_not_traded_fills() -> None:
    bars = _bars()
    baseline = _engine().run_prepared(_PreparedBars(bars))
    mark = _engine(_mark_source(bars, close_offset=25.0)).run_prepared(_PreparedBars(bars))

    try:
        assert mark.journal.fills == baseline.journal.fills
        assert mark.journal.fills[0].price == pytest.approx(bars[1].open)
        mark_equity = tuple(mark.journal.equity)
        baseline_equity = tuple(baseline.journal.equity)
        assert mark_equity != baseline_equity
        open_pairs = tuple(
            (mark_point, baseline_point)
            for mark_point, baseline_point in zip(mark_equity, baseline_equity, strict=True)
            if mark_point.position_open and baseline_point.position_open
        )
        assert open_pairs
        assert any(
            mark_point.equity > baseline_point.equity for mark_point, baseline_point in open_pairs
        )
    finally:
        baseline.journal.streaming_store.cleanup()
        mark.journal.streaming_store.cleanup()


@pytest.mark.checkpoint
def test_real_runtime_snapshot_restores_mark_broker_exactly() -> None:
    bars = _bars()
    mark_source = _mark_source(bars, close_offset=15.0)
    direct_source = _PreparedBars(bars)
    restart_source = _PreparedBars(bars)
    factory = _mark_factory(mark_source)

    direct = factory().run_prepared(direct_source)
    replay = run_with_runtime_snapshot_restart(
        factory,
        restart_source,
        restart_at_index=5,
        seed=23,
    )

    try:
        assert replay.restart_verified is True
        assert replay.checkpoint is not None
        assert replay.checkpoint.snapshot_schema_version == "1"
        assert replay.source_rows_replayed == 0
        assert _result_digest(replay.result) == _result_digest(direct)
        assert replay.result.journal.fills == direct.journal.fills
        assert tuple(replay.result.journal.equity) == tuple(direct.journal.equity)
    finally:
        direct.journal.streaming_store.cleanup()
        replay.result.journal.streaming_store.cleanup()


def _config(tmp_path: Path) -> BacktestAppConfig:
    return BacktestAppConfig(
        data=DataSourceConfig(
            path=tmp_path / "bars.csv",
            format="csv",
            symbol="BTCUSDT",
        ),
        modules=tmp_path / "modules.yaml",
        requested_timeframes=(),
        output=OutputConfig(tmp_path / "results"),
        seed=7,
    )


def _validation(config: BacktestAppConfig, bars: tuple[Candle, ...]) -> ValidationSummary:
    return ValidationSummary(
        path=str(config.data.path),
        sha256="abc",
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=len(bars),
        first_timestamp=bars[0].timestamp.isoformat(),
        last_timestamp=bars[-1].timestamp.isoformat(),
        gaps=0,
        duplicates=0,
        requested_start=None,
        requested_end=None,
    )


def _provenance() -> SourceProvenance:
    return SourceProvenance(
        application_version="0.1.0",
        distribution_version="0.4.2",
        application_source_sha256="a" * 64,
        core_source_sha256="b" * 64,
        application_identity_sha256="c" * 64,
        source_commit_sha="d" * 40,
    )


class _FakeMarkResult:
    def __init__(self) -> None:
        self.journal = SimpleNamespace(trades=[], fills=[], equity=[])
        self.run_manifest = SimpleNamespace(run_id="run-mark-test")
        self.bars_processed = 20

    def export_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "run.json").write_text(
            json.dumps({"manifest": {"run_id": self.run_manifest.run_id}}),
            encoding="utf-8",
        )
        (path / "metrics.json").write_text("{}", encoding="utf-8")
        return path


class _FakeReplay:
    checkpoint = None
    restart_verified = False
    continuous_sha256 = None
    restarted_sha256 = None
    recovery_mode = "disabled"
    simulation_passes = 1
    source_traversals = 1
    source_rows_replayed = 0
    result = _FakeMarkResult()

    def evidence(self) -> dict[str, object]:
        return {
            "restart_verified": False,
            "recovery_mode": "disabled",
            "simulation_passes": 1,
            "source_traversals": 1,
            "source_rows_replayed": 0,
            "continuous_sha256": None,
            "restarted_sha256": None,
            "checkpoint": None,
        }


def test_runner_publishes_mark_identity_and_evidence_without_changing_core_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars()
    mark_source = _mark_source(bars)
    config = _config(tmp_path)
    validation = _validation(config, bars)
    prepared = SimpleNamespace(
        total_bars=len(bars),
        first_candle=bars[0],
        last_candle=bars[-1],
        validation=validation,
    )
    monkeypatch.setattr(runner, "prepare_candle_source", lambda _data: prepared)
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)

    def fake_single_pass(
        _config: BacktestAppConfig,
        _source: object,
        _performance: PerformanceConfig,
        _funding_source: object = None,
        *,
        valuation_policy: str,
        mark_price_source: RegisteredMarkPriceSource | None,
    ) -> ReplayRun:
        assert valuation_policy == VALUATION_POLICY_MARK
        assert mark_price_source is mark_source
        return _FakeReplay()  # type: ignore[return-value]

    monkeypatch.setattr(runner, "_run_single_pass", fake_single_pass)

    outcome = runner.run_backtest(
        config,
        valuation_policy=VALUATION_POLICY_MARK,
        mark_price_source=mark_source,
    )
    repeated = runner.run_backtest(
        config,
        valuation_policy=VALUATION_POLICY_MARK,
        mark_price_source=mark_source,
    )

    assert outcome.run_id == "run-mark-test"
    assert outcome.application_run_id is not None
    assert outcome.application_run_id.startswith("run-mark-test-mark-")
    assert outcome.output_directory.name == outcome.application_run_id
    assert repeated.output_directory == outcome.output_directory

    application = json.loads((outcome.output_directory / "application-run.json").read_text())
    assert application["core_run_id"] == "run-mark-test"
    assert application["application_run_id"] == outcome.application_run_id
    assert application["valuation_model"] == MARK_VALUATION_MODEL_VERSION
    assert application["mark_source_identity_sha256"] == mark_source.identity_sha256()
    assert application["execution_price_source"] == "traded_ohlc"
    assert application["execution_risk_reference"] == "mark_open"
    assert application["post_bar_reference"] == "mark_close"
    assert "uri" not in str(application["mark_source"]).lower()

    runtime = json.loads((outcome.output_directory / "valuation-runtime.json").read_text())
    assert runtime["application_run_id"] == outcome.application_run_id
    assert runtime["mark_source_identity_sha256"] == mark_source.identity_sha256()

    manifest = verify_artifact_manifest(outcome.output_directory)
    assert manifest["run_id"] == "run-mark-test"
    assert manifest["application_run_id"] == outcome.application_run_id
    assert "valuation-runtime.json" in manifest["files"]
    inspected = inspect_result(outcome.output_directory)
    assert inspected.run_id == "run-mark-test"
    assert inspected.application_run_id == outcome.application_run_id


def test_runner_fails_before_execution_when_mark_source_does_not_match_candles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _bars()
    mismatched = _mark_source(bars[:-1])
    config = _config(tmp_path)
    validation = _validation(config, bars)
    prepared = SimpleNamespace(
        total_bars=len(bars),
        first_candle=bars[0],
        last_candle=bars[-1],
        validation=validation,
    )
    monkeypatch.setattr(runner, "prepare_candle_source", lambda _data: prepared)
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)
    monkeypatch.setattr(
        runner,
        "_run_single_pass",
        lambda *_args, **_kwargs: pytest.fail("execution must not start for mismatched mark data"),
    )

    with pytest.raises(MarkValuationError, match="row count"):
        runner.run_backtest(
            config,
            valuation_policy=VALUATION_POLICY_MARK,
            mark_price_source=mismatched,
        )
