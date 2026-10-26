from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from modular_core import BacktestEngine, Candle, ModuleRegistry, Side

EXPECTED_EXTERNAL_ENTRY_POINTS = frozenset({"session_range", "market_regime", "fvg"})
EXPECTED_MODULES = frozenset(
    {"session_range", "breakout_return", "market_regime", "fvg", "liquidity_sweep"}
)


def build_synthetic_bars(*, include_return: bool) -> list[Candle]:
    """Build a closed-bar NY range -> breakout -> optional return scenario.

    2026-01-05 05:00 UTC is 00:00 America/New_York. The first 48 five-minute
    bars therefore form the complete 00:00-04:00 NY range without DST
    ambiguity. The breakout and return happen only after the range closes.
    """

    start = datetime(2026, 1, 5, 5, 0, tzinfo=timezone.utc)
    bars = [
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(48)
    ]

    bars.append(
        Candle(
            timestamp=start + timedelta(minutes=5 * 48),
            open=102.0,
            high=103.0,
            low=101.5,
            close=102.0,
            volume=2.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
    )

    if include_return:
        bars.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * 49),
                open=100.9,
                high=101.1,
                low=100.7,
                close=100.9,
                volume=2.0,
                symbol="BTCUSDT",
                timeframe="5m",
            )
        )
        for index in range(50, 54):
            bars.append(
                Candle(
                    timestamp=start + timedelta(minutes=5 * index),
                    open=100.8,
                    high=101.0,
                    low=100.5,
                    close=100.7,
                    volume=1.0,
                    symbol="BTCUSDT",
                    timeframe="5m",
                )
            )
    return bars


def _load_plan(config_path: Path):
    registry = ModuleRegistry()
    modules = registry.load_config(config_path)
    return registry.validate_and_plan(modules, base_timeframe="5m")


def _breakout_sell_signals(result: Any) -> list[Any]:
    return [
        row["signal"]
        for row in result.journal.signals
        if row["signal"].module == "breakout_return" and row["signal"].side is Side.SELL
    ]


def run_smoke(config_path: Path) -> dict[str, Any]:
    discovery = ModuleRegistry()
    discovered = frozenset(discovery.discover_entry_points())
    missing_entry_points = sorted(EXPECTED_EXTERNAL_ENTRY_POINTS - discovered)
    if missing_entry_points:
        raise RuntimeError(f"missing external module entry points: {missing_entry_points}")

    plan = _load_plan(config_path)
    ordered_names = tuple(module.name for module in plan.modules)
    if frozenset(ordered_names) != EXPECTED_MODULES:
        raise RuntimeError(f"unexpected resolved modules: {ordered_names}")
    if ordered_names.index("session_range") >= ordered_names.index("breakout_return"):
        raise RuntimeError("dependency planner did not place session_range before breakout_return")
    if plan.produced_by.get("session_range.completed") != "session_range":
        raise RuntimeError("session_range.completed producer contract was not resolved")
    if plan.required_timeframes != ("15m",):
        raise RuntimeError(f"unexpected timeframe plan: {plan.required_timeframes}")

    before_return = BacktestEngine(_load_plan(config_path).modules).run(
        build_synthetic_bars(include_return=False)
    )
    if _breakout_sell_signals(before_return):
        raise RuntimeError("look-ahead regression: signal exists before the return bar closes")
    if before_return.journal.errors:
        raise RuntimeError(f"pre-return engine errors: {before_return.journal.errors}")

    completed = BacktestEngine(_load_plan(config_path).modules).run(
        build_synthetic_bars(include_return=True)
    )
    sell_signals = _breakout_sell_signals(completed)
    if len(sell_signals) != 1:
        raise RuntimeError(
            f"expected one confirmed Breakout-Return SELL signal, got {len(sell_signals)}"
        )
    if completed.journal.errors:
        raise RuntimeError(f"full integration engine errors: {completed.journal.errors}")

    return {
        "status": "ok",
        "entry_points": sorted(discovered & EXPECTED_EXTERNAL_ENTRY_POINTS),
        "modules": list(ordered_names),
        "required_timeframes": list(plan.required_timeframes),
        "events": len(completed.journal.events),
        "features": len(completed.journal.features),
        "setups": len(completed.journal.setups),
        "signals": len(completed.journal.signals),
        "breakout_sell_signals": len(sell_signals),
        "run_id": completed.run_manifest.run_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the deterministic all-module integration smoke test"
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path("configs/integration.example.yaml"),
    )
    args = parser.parse_args()
    report = run_smoke(args.config)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
