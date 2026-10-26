from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import runner
from modular_backtest.config import BacktestAppConfig, DataSourceConfig, OutputConfig
from modular_backtest.data import ValidationSummary
from modular_backtest.funding import FundingEvent, FundingLineageEntry, RegisteredFundingSource
from modular_backtest.funding_application import funding_execution_factory
from modular_backtest.futures_execution import FUNDING_RUNTIME_MODEL_VERSION
from modular_backtest.provenance import SourceProvenance
from modular_backtest.recovery import _result_digest, run_with_runtime_snapshot_restart
from modular_backtest.results import inspect_result, verify_artifact_manifest
from modular_backtest.scalability import PerformanceConfig, ScalableBacktestEngine
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_core.config import DataConfig, EngineConfig
from modular_core.instruments import InstrumentSpec
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule


START = datetime(2026, 1, 1, tzinfo=UTC)
COVERAGE_END = START + timedelta(hours=2)


def _funding_source(*, dataset_hash: str = "c" * 64) -> RegisteredFundingSource:
    events = (
        FundingEvent(
            ordinal=0,
            timestamp=START,
            symbol="BTCUSDT",
            rate=Decimal("0.01"),
            mark_price=Decimal("100"),
        ),
        FundingEvent(
            ordinal=1,
            timestamp=START + timedelta(minutes=40),
            symbol="BTCUSDT",
            rate=Decimal("0.005"),
            mark_price=Decimal("108"),
        ),
    )
    return RegisteredFundingSource(
        dataset_id="binance-btcusdt-funding",
        dataset_version=12,
        dataset_hash=dataset_hash,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=COVERAGE_END,
        event_count=2,
        source_record_count=2,
        lineage=(
            FundingLineageEntry(
                artifact_id="funding-v3-owner",
                source_kind="official_rest",
                checksum_sha256="a" * 64,
                record_count=2,
                start=START,
                end=COVERAGE_END,
            ),
        ),
        events=events,
    )


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


def _validation(config: BacktestAppConfig) -> ValidationSummary:
    return ValidationSummary(
        path=str(config.data.path),
        sha256="abc",
        symbol="BTCUSDT",
        market_type="binance-usdt-m-perpetual",
        base_timeframe="5m",
        rows=20,
        first_timestamp=START.isoformat(),
        last_timestamp=(START + timedelta(minutes=95)).isoformat(),
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


class _FakeFundingResult:
    def __init__(self, source: RegisteredFundingSource) -> None:
        self.source = source
        self.journal = SimpleNamespace(trades=[], fills=[], equity=[])
        self.run_manifest = SimpleNamespace(run_id="run-test")
        self.bars_processed = 20

    def export_directory(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "run.json").write_text(
            json.dumps({"manifest": {"run_id": "run-test"}}),
            encoding="utf-8",
        )
        (path / "metrics.json").write_text("{}", encoding="utf-8")
        source_identity = self.source.identity_sha256()
        event = self.source.events[1]
        row = {
            "event_id": "evt-0000000002",
            "run_id": "run-test",
            "sequence": 2,
            "timestamp": event.timestamp.isoformat(),
            "event_type": "portfolio.funding",
            "source": "backtest_funding",
            "payload": {
                "funding": {
                    "model_version": FUNDING_RUNTIME_MODEL_VERSION,
                    "source_identity_sha256": source_identity,
                    "ordinal": event.ordinal,
                    "event_key": event.key(source_identity),
                    "cashflow": "-0.540",
                }
            },
            "symbol": "BTCUSDT",
        }
        (path / "events.jsonl").write_text(
            json.dumps(row, sort_keys=True) + "\n",
            encoding="utf-8",
        )
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

    def __init__(self, source: RegisteredFundingSource) -> None:
        self.result = _FakeFundingResult(source)

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


def test_funding_runner_binds_output_and_manifest_to_application_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    validation = _validation(config)
    prepared = SimpleNamespace(total_bars=20, validation=validation)
    monkeypatch.setattr(runner, "prepare_candle_source", lambda data: prepared)
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)

    def fake_single_pass(
        config: BacktestAppConfig,
        source: object,
        performance: PerformanceConfig,
        funding_source: RegisteredFundingSource | None = None,
    ) -> _FakeReplay:
        del config, source, performance
        assert funding_source is not None
        return _FakeReplay(funding_source)

    monkeypatch.setattr(runner, "_run_single_pass", fake_single_pass)

    funding = _funding_source()
    outcome = runner.run_backtest(config, funding_source=funding)
    repeated = runner.run_backtest(config, funding_source=funding)

    assert outcome.run_id == "run-test"
    assert outcome.application_run_id is not None
    assert outcome.application_run_id != outcome.run_id
    assert outcome.application_run_id.startswith("run-test-funding-")
    assert outcome.output_directory.name == outcome.application_run_id
    assert repeated.output_directory == outcome.output_directory

    application = json.loads((outcome.output_directory / "application-run.json").read_text())
    assert application["core_run_id"] == "run-test"
    assert application["application_run_id"] == outcome.application_run_id
    assert application["funding_model"] == FUNDING_RUNTIME_MODEL_VERSION
    assert application["funding_source_identity_sha256"] == funding.identity_sha256()
    assert "uri" not in str(application["funding_source"]).lower()

    runtime = json.loads((outcome.output_directory / "funding-runtime.json").read_text())
    assert runtime["application_run_id"] == outcome.application_run_id
    assert runtime["source_identity_sha256"] == funding.identity_sha256()
    assert runtime["journaled_settlement_count"] == 1
    assert runtime["first_journaled_ordinal"] == 1
    assert runtime["last_journaled_ordinal"] == 1
    assert Decimal(runtime["cumulative_journaled_cashflow"]) == Decimal("-0.540")

    manifest = verify_artifact_manifest(outcome.output_directory)
    assert manifest["run_id"] == "run-test"
    assert manifest["application_run_id"] == outcome.application_run_id
    assert "funding-runtime.json" in manifest["files"]
    inspected = inspect_result(outcome.output_directory)
    assert inspected.run_id == "run-test"
    assert inspected.application_run_id == outcome.application_run_id


def test_different_funding_owner_identity_cannot_reuse_application_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    validation = _validation(config)
    prepared = SimpleNamespace(total_bars=20, validation=validation)
    monkeypatch.setattr(runner, "prepare_candle_source", lambda data: prepared)
    monkeypatch.setattr(runner, "build_source_provenance", _provenance)

    def fake_single_pass(
        config: BacktestAppConfig,
        source: object,
        performance: PerformanceConfig,
        funding_source: RegisteredFundingSource | None = None,
    ) -> _FakeReplay:
        del config, source, performance
        assert funding_source is not None
        return _FakeReplay(funding_source)

    monkeypatch.setattr(runner, "_run_single_pass", fake_single_pass)

    left = runner.run_backtest(config, funding_source=_funding_source(dataset_hash="c" * 64))
    right = runner.run_backtest(config, funding_source=_funding_source(dataset_hash="d" * 64))

    assert left.run_id == right.run_id == "run-test"
    assert left.application_run_id != right.application_run_id
    assert left.output_directory != right.output_directory
    assert left.output_directory.is_dir()
    assert right.output_directory.is_dir()


class FundingEntryModule(StrategyModule):
    name = "funding_entry_v3"
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
                    client_order_id="funding-v3-entry",
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


def _bars(count: int = 20) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            timestamp=START + timedelta(minutes=5 * index),
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


def _funding_scalable_factory(source: RegisteredFundingSource):
    def create() -> ScalableBacktestEngine:
        performance = PerformanceConfig(
            mode="canonical_audit",
            max_memory_mb=1024,
            progress_interval_bars=10_000,
            max_base_history=64,
            max_timeframe_history=64,
        )
        return ScalableBacktestEngine(
            FundingEntryModule(),
            EngineConfig(
                close_open_position_at_end=True,
                data=DataConfig(
                    expected_interval_seconds=300,
                    gap_policy="reject",
                    max_base_history=64,
                ),
            ),
            instrument=InstrumentSpec(symbol="BTCUSDT", contract_size=1.0),
            execution_factory=funding_execution_factory(source),
            performance=performance,
        )

    return create


@pytest.mark.checkpoint
def test_real_runtime_snapshot_restores_funding_broker_exactly() -> None:
    funding = _funding_source()
    bars = _bars()
    direct_source = _PreparedBars(bars)
    restart_source = _PreparedBars(bars)
    factory = _funding_scalable_factory(funding)

    direct = factory().run_prepared(direct_source)
    replay = run_with_runtime_snapshot_restart(
        factory,
        restart_source,
        restart_at_index=5,
        seed=17,
    )

    try:
        assert replay.restart_verified is True
        assert replay.checkpoint is not None
        assert replay.checkpoint.snapshot_schema_version == "1"
        assert replay.source_rows_replayed == 0
        assert _result_digest(replay.result) == _result_digest(direct)
        direct_funding = [
            event for event in direct.journal.events if event.event_type == "portfolio.funding"
        ]
        restarted_funding = [
            event
            for event in replay.result.journal.events
            if event.event_type == "portfolio.funding"
        ]
        assert len(direct_funding) == len(restarted_funding) == 2
        assert direct_funding == restarted_funding
        assert Decimal(direct_funding[-1].payload["funding"]["cashflow"]) != Decimal("0")
    finally:
        direct.journal.streaming_store.cleanup()
        replay.result.journal.streaming_store.cleanup()
