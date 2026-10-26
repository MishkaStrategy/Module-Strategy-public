from __future__ import annotations

import pytest

from modular_backtest.scalability import deadline as deadline_module
from modular_backtest.scalability.config import DurationLimitExceeded, RuntimeCounters
from modular_backtest.scalability.deadline import (
    check_execution_deadline,
    deadline_from_now,
    execution_deadline,
)
from modular_backtest.scalability.memory import MemoryGuard


def _counters(*, bars: int = 17) -> RuntimeCounters:
    return RuntimeCounters(
        bars_processed=bars,
        events=3,
        skip_ranges=1,
        equity_points=5,
        retained_equity_points=5,
        diagnostics=0,
        trades=0,
        rss_bytes=1,
        peak_rss_bytes=1,
    )


def test_deadline_from_now_requires_positive_finite_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: 100.0)

    assert deadline_from_now(12) == 112.0
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            deadline_from_now(value)
    with pytest.raises(TypeError):
        deadline_from_now(True)


def test_execution_deadline_is_scoped_and_restores_outer_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"value": 5.0}
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: now["value"])
    counters = _counters()

    with execution_deadline(10.0):
        check_execution_deadline(counters)
        with execution_deadline(6.0):
            now["value"] = 6.0
            with pytest.raises(DurationLimitExceeded) as exc_info:
                check_execution_deadline(counters)
            assert exc_info.value.counters is counters
        now["value"] = 9.0
        check_execution_deadline(counters)

    now["value"] = 100.0
    check_execution_deadline(counters)


def test_memory_guard_checks_deadline_even_without_memory_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: 50.0)
    counters = _counters(bars=2048)

    with execution_deadline(49.0):
        with pytest.raises(DurationLimitExceeded, match="execution deadline exceeded") as exc_info:
            MemoryGuard(None).check(counters)

    assert exc_info.value.counters is counters
    assert "bars=2048" in str(exc_info.value)


def test_execution_deadline_rejects_invalid_absolute_value() -> None:
    for value in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            with execution_deadline(value):
                pass
    with pytest.raises(TypeError):
        with execution_deadline(True):
            pass
