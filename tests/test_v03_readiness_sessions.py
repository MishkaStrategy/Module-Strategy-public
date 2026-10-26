from datetime import UTC, datetime, time

from modular_core import (
    BacktestEngine,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    SessionDefinition,
    StrategyModule,
)


class FifteenMinuteModule(StrategyModule):
    name = "fifteen"
    manifest = ModuleManifest(
        name=name,
        required_timeframes=("15m",),
        warmup_bars={"15m": 1},
    )

    def __init__(self):
        self.called = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.called.append((view.index, len(view.timeframe_bars["15m"]), view.readiness.is_ready))
        return ModuleDecision()


def test_required_timeframe_is_auto_built_and_module_waits_for_warmup(bars_factory):
    module = FifteenMinuteModule()
    result = BacktestEngine(module).run(bars_factory([100, 101, 102, 103]))
    assert module.called == [(2, 1, True), (3, 1, True)]
    assert sum("module_not_ready" in row["reason"] for row in result.journal.skips) == 2


class SessionProbe(StrategyModule):
    name = "session_probe"

    def __init__(self):
        self.open = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.open.append(view.sessions.is_open("ny"))
        return ModuleDecision()


def test_session_view_is_dst_aware(bars_factory):
    bars = bars_factory([100])
    # 2026-01-01 00:05 UTC is 19:05 previous day in New York.
    session = SessionDefinition("ny", "America/New_York", time(19, 0), time(20, 0))
    module = SessionProbe()
    BacktestEngine(module, sessions=(session,)).run(bars)
    assert module.open == [True]


def test_candle_timestamp_is_open_time_and_close_is_derived(bars_factory):
    bar = bars_factory([100])[0]
    assert bar.open_time == bar.timestamp
    assert (bar.close_time - bar.open_time).total_seconds() == 300


def test_calendar_aligned_daily_bucket_handles_dst_short_day():
    from datetime import timedelta

    from modular_core import Candle, TimeframeAggregator

    start = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # midnight New York
    bars = [
        Candle(
            start + timedelta(hours=i),
            100,
            101,
            99,
            100,
            1,
            "BTCUSDT",
            "1h",
        )
        for i in range(23)
    ]
    agg = TimeframeAggregator(
        24,
        "1d-ny",
        expected_base_interval=timedelta(hours=1),
        align_to_clock=True,
        alignment_timezone="America/New_York",
    )
    outputs = [value for bar in bars if (value := agg.push(bar)) is not None]
    assert len(outputs) == 1
    assert outputs[0].volume == 23
