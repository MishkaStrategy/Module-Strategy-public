from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_backtest.scalability import (
    CompactEquitySeries,
    EquitySampler,
    MemoryGuard,
    MemoryLimitExceeded,
    ObservabilityLimitExceeded,
    PerformanceConfig,
    RuntimeCounters,
    ScalableBacktestEngine,
    ScalableJournal,
    StreamingEventStore,
)
from modular_core.config import DataConfig, EngineConfig
from modular_core.engine import BacktestEngine
from modular_core.events import EventEnvelope
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, EquityPoint, OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule


def _point(index: int, equity: float = 10_000.0, *, position_open: bool = False) -> EquityPoint:
    return EquityPoint(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
        cash=equity,
        unrealized_pnl=0.0,
        equity=equity,
        position_open=position_open,
    )


def _event(sequence: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt-{sequence:010d}",
        run_id="run-test",
        sequence=sequence,
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="test.event",
        source="test",
        payload={"sequence": sequence},
    )


def test_compact_equity_preserves_exact_values_and_last_point_update() -> None:
    series = CompactEquitySeries()
    first = _point(0)
    second = _point(1, 10_025.5, position_open=True)
    series.append(first)
    series.append(second)

    assert list(series) == [first, second]
    replacement = _point(1, 10_030.0)
    series[-1] = replacement
    assert len(series) == 2
    assert series[-1] == replacement


def test_streaming_event_store_retains_only_bounded_tail(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl", retained_window=2)
    store.append_many((_event(1), _event(2), _event(3)))

    assert store.count == 3
    assert [event.sequence for event in store.read()] == [2, 3]
    assert store.retained_count == 2
    assert store.counts_by_type == {"test.event": 3}
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 3
    with pytest.raises(ValueError, match="contiguous"):
        store.append(_event(5))
    store.cleanup()


def test_repeated_skip_is_aggregated_and_reason_change_closes_range(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(max_memory_mb=None),
    )
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    for index in range(3):
        journal.set_bar_context(index)
        journal.log_skip(
            timestamp + timedelta(minutes=5 * index),
            "module-a",
            "module_not_ready:15m",
            symbol="BTCUSDT",
        )
    journal.set_bar_context(3)
    journal.log_skip(
        timestamp + timedelta(minutes=15),
        "module-a",
        "missing_feature:trend",
        symbol="BTCUSDT",
    )
    journal.close_skip_range("module-a", timestamp + timedelta(minutes=20))

    assert len(journal.skips) == 2
    assert journal.skips[0]["count"] == 3
    assert journal.skips[0]["first_bar_index"] == 0
    assert journal.skips[0]["last_bar_index"] == 2
    assert journal.skips[0]["last_timestamp"] == timestamp + timedelta(minutes=10)
    assert journal.skips[1]["count"] == 1
    assert store.counts_by_type == {"module.skip_range": 2}
    store.cleanup()


def test_readiness_close_does_not_extend_last_skipped_timestamp(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(max_memory_mb=None),
    )
    timestamp = datetime(2024, 1, 1, tzinfo=UTC)
    journal.set_bar_context(4)
    journal.log_skip(timestamp, "module-a", "module_not_ready:1d")
    journal.set_bar_context(5)
    journal.close_skip_range("module-a", timestamp + timedelta(minutes=5))

    assert journal.skips[0]["last_timestamp"] == timestamp
    assert journal.skips[0]["last_bar_index"] == 4
    store.cleanup()


def test_equity_is_exact_without_portfolio_mark_event(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(
            mode="research_fast",
            max_memory_mb=None,
            equity_sampling="every_n_bars",
            equity_every_n_bars=2,
        ),
    )
    for index in range(5):
        journal.set_bar_context(index)
        journal.record_equity(_point(index, 10_000.0 + index))
    journal.finalize_observability()

    assert len(journal.equity) == 5
    assert journal.equity[-1].equity == 10_004.0
    assert len(journal.retained_equity) == 3
    assert store.count == 0
    assert "portfolio.mark" not in store.counts_by_type
    store.cleanup()


def test_research_fast_drops_per_bar_diagnostics(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(mode="research_fast", max_memory_mb=None),
    )
    journal.log_diagnostics(datetime(2024, 1, 1, tzinfo=UTC), "module", {"x": 1})

    assert journal.diagnostics == []
    assert store.count == 0
    output = journal.export_directory(tmp_path / "result")
    payload = json.loads((output / "journal.json").read_text())
    assert payload["dropped_diagnostics"] == 1
    store.cleanup()


def test_equity_sampling_policies_keep_required_points() -> None:
    sampler = EquitySampler("on_trade", 12)
    assert not sampler.accept(_point(0), bar_index=0, trade_count_changed=False)
    assert sampler.accept(_point(1), bar_index=1, trade_count_changed=True)

    hybrid = EquitySampler("hybrid", 12)
    assert hybrid.accept(_point(0), bar_index=0, trade_count_changed=False)
    assert hybrid.accept(
        _point(1, position_open=True),
        bar_index=1,
        trade_count_changed=False,
    )


def test_memory_guard_fails_closed_with_counters() -> None:
    counters = RuntimeCounters(
        bars_processed=100,
        events=10,
        skip_ranges=2,
        equity_points=100,
        retained_equity_points=10,
        diagnostics=0,
        trades=1,
        rss_bytes=2 * 1024 * 1024,
        peak_rss_bytes=2 * 1024 * 1024,
    )
    with pytest.raises(MemoryLimitExceeded) as raised:
        MemoryGuard(1).check(counters)
    assert raised.value.counters == counters


def test_memory_guard_ignores_stale_process_peak() -> None:
    counters = RuntimeCounters(
        bars_processed=100,
        events=2,
        skip_ranges=1,
        equity_points=100,
        retained_equity_points=10,
        diagnostics=0,
        trades=0,
        rss_bytes=512 * 1024,
        peak_rss_bytes=2 * 1024 * 1024,
    )

    MemoryGuard(1).check(counters)


def test_limits_and_mode_defaults_are_validated() -> None:
    assert PerformanceConfig(mode="canonical_audit").equity_sampling == "every_bar"
    assert PerformanceConfig(mode="research_fast").equity_sampling == "hybrid"
    with pytest.raises(ValueError, match="max_events"):
        PerformanceConfig(max_events=0)
    with pytest.raises(ValueError, match="unsupported equity"):
        PerformanceConfig(equity_sampling="invalid")


def test_event_limit_rejects_incomplete_observability(tmp_path: Path) -> None:
    store = StreamingEventStore(tmp_path / "events.jsonl")
    journal = ScalableJournal(
        run_id="run-test",
        event_store=store,
        performance=PerformanceConfig(max_events=1, max_memory_mb=None),
    )
    journal.emit_event(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        event_type="one",
        source="test",
        payload={},
    )
    with pytest.raises(ObservabilityLimitExceeded, match="max_events"):
        journal.emit_event(
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            event_type="two",
            source="test",
            payload={},
        )
    store.cleanup()


class _WarmupNoop(StrategyModule):
    name = "warmup_noop"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        warmup_bars={"5m": 3},
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(diagnostics={"index": view.index})


def _bars(count: int) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(count)
    ]


def test_scalable_engine_aggregates_warmup_skips_and_keeps_exact_equity() -> None:
    engine = ScalableBacktestEngine(
        _WarmupNoop(),
        EngineConfig(data=DataConfig(expected_interval_seconds=300, gap_policy="reject")),
        performance=PerformanceConfig(
            mode="research_fast",
            max_memory_mb=None,
            progress_interval_bars=100,
        ),
    )
    result = engine.run(_bars(5))
    journal = result.journal
    assert isinstance(journal, ScalableJournal)
    assert result.bars_processed == 5
    assert len(journal.equity) == 5
    assert len(journal.skips) == 1
    assert journal.skips[0]["reason_code"] == "module_not_ready"
    assert journal.skips[0]["count"] == 2
    assert "portfolio.mark" not in journal.streaming_store.counts_by_type
    assert journal.diagnostics == []
    journal.streaming_store.cleanup()


def test_scalable_engine_resource_limit_is_fail_closed() -> None:
    engine = ScalableBacktestEngine(
        _WarmupNoop(),
        EngineConfig(data=DataConfig(expected_interval_seconds=300, gap_policy="reject")),
        performance=PerformanceConfig(
            max_memory_mb=None,
            max_events=1,
            progress_interval_bars=100,
        ),
    )
    with pytest.raises(ObservabilityLimitExceeded) as raised:
        engine.run(_bars(3))
    assert raised.value.counters is not None


class _SingleTradeModule(StrategyModule):
    name = "single_trade_equivalence"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index != 0:
            return ModuleDecision()
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    stop_loss=95.0,
                    take_profit=101.0,
                    client_order_id="equivalence-entry",
                ),
            )
        )


def test_scalable_engine_preserves_trade_and_critical_event_semantics() -> None:
    config = EngineConfig(
        close_open_position_at_end=True,
        data=DataConfig(expected_interval_seconds=300, gap_policy="reject"),
    )
    bars = _bars(8)
    legacy = BacktestEngine(_SingleTradeModule(), config).run(bars)
    scalable = ScalableBacktestEngine(
        _SingleTradeModule(),
        config,
        performance=PerformanceConfig(
            mode="research_fast",
            max_memory_mb=None,
            progress_interval_bars=100,
        ),
    ).run(bars)
    scalable_journal = scalable.journal
    assert isinstance(scalable_journal, ScalableJournal)

    assert scalable.metrics == legacy.metrics
    assert list(scalable_journal.fills) == list(legacy.journal.fills)
    assert list(scalable_journal.trades) == list(legacy.journal.trades)
    assert list(scalable_journal.equity) == list(legacy.journal.equity)
    assert [
        (event.timestamp, event.event, event.order, event.reason)
        for event in scalable_journal.orders
    ] == [
        (event.timestamp, event.event, event.order, event.reason) for event in legacy.journal.orders
    ]
    legacy_critical_types = [
        event.event_type for event in legacy.journal.events if event.event_type != "portfolio.mark"
    ]
    assert [event.event_type for event in scalable_journal.events] == legacy_critical_types
    scalable_journal.streaming_store.cleanup()
