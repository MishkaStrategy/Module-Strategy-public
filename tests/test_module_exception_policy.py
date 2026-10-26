from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    MarketView,
    ModuleDecision,
    StrategyModule,
)

BAR = Candle(datetime(2026, 1, 1, tzinfo=timezone.utc), 100, 101, 99, 100, 1, "X", "5m")


class CountingModule(StrategyModule):
    def __init__(
        self,
        name: str,
        *,
        fail_start: bool = False,
        fail_bar: bool = False,
        fail_finish: bool = False,
    ) -> None:
        self.name = name
        self.fail_start = fail_start
        self.fail_bar = fail_bar
        self.fail_finish = fail_finish
        self.starts = 0
        self.bars = 0
        self.finishes = 0

    def on_start(self) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("start failed")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.bars += 1
        if self.fail_bar:
            raise ValueError("bar failed")
        return ModuleDecision()

    def on_finish(self) -> None:
        self.finishes += 1
        if self.fail_finish:
            raise LookupError("finish failed")


def test_non_strict_start_and_bar_errors_have_stable_skip_reasons() -> None:
    start_failure = CountingModule("start_failure", fail_start=True)
    bar_failure = CountingModule("bar_failure", fail_bar=True)
    healthy = CountingModule("healthy")
    result = BacktestEngine(
        (start_failure, bar_failure, healthy),
        EngineConfig(strict_module_errors=False),
    ).run((BAR,))

    reasons = {(row["module"], row["reason"]) for row in result.journal.skips}
    assert ("start_failure", "module_start_error:RuntimeError") in reasons
    assert ("bar_failure", "module_runtime_error:ValueError") in reasons
    assert start_failure.finishes == 0
    assert bar_failure.finishes == 1
    assert healthy.bars == healthy.finishes == 1


def test_strict_finish_failure_is_not_silently_swallowed() -> None:
    module = CountingModule("finish_failure", fail_finish=True)
    with pytest.raises(ExceptionGroup, match="module finish failures") as captured:
        BacktestEngine(module).run((BAR,))
    assert len(captured.value.exceptions) == 1
    assert isinstance(captured.value.exceptions[0], LookupError)


def test_finish_failure_does_not_mask_primary_runtime_error() -> None:
    module = CountingModule("both", fail_bar=True, fail_finish=True)
    with pytest.raises(ValueError, match="bar failed"):
        BacktestEngine(module).run((BAR,))
    assert module.finishes == 1


def test_non_strict_finish_failure_is_logged_and_result_is_returned() -> None:
    module = CountingModule("finish_failure", fail_finish=True)
    result = BacktestEngine(module, EngineConfig(strict_module_errors=False)).run((BAR,))
    assert any(row["reason"] == "module_finish_error:LookupError" for row in result.journal.skips)
