from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigurationError, load_config
from .data import DataValidationError
from .results import ResultError, compare_results, inspect_result
from .runner import run_backtest
from .scalability import PerformanceConfig, ResourceLimitExceeded
from .streaming_data import prepare_candle_source


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _optional_limit(value: int | None) -> int | None:
    return None if value == 0 else value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="modular-backtest")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-data", help="validate OHLCV input")
    validate.add_argument("--config", required=True, type=Path)

    run = subparsers.add_parser("run", help="run a deterministic backtest")
    run.add_argument("--config", required=True, type=Path)
    run.add_argument(
        "--mode",
        choices=("canonical_audit", "research_fast"),
        default="canonical_audit",
        help="observability mode; trading semantics are identical",
    )
    run.add_argument("--max-memory-mb", type=_nonnegative_int, default=9216)
    run.add_argument("--max-events", type=_nonnegative_int, default=2_000_000)
    run.add_argument("--max-skip-ranges", type=_nonnegative_int, default=250_000)
    run.add_argument("--max-diagnostics", type=_nonnegative_int, default=250_000)
    run.add_argument("--max-equity-points", type=_nonnegative_int, default=5_000_000)
    run.add_argument("--progress-interval-bars", type=int, default=10_000)
    run.add_argument("--retained-event-window", type=int, default=2_000)
    run.add_argument(
        "--equity-sampling",
        choices=(
            "every_bar",
            "every_n_bars",
            "hourly",
            "daily",
            "on_change",
            "on_trade",
            "hybrid",
        ),
        default=None,
    )
    run.add_argument("--equity-every-n-bars", type=int, default=12)
    run.add_argument("--max-base-history", type=_nonnegative_int, default=65_536)
    run.add_argument("--max-timeframe-history", type=_nonnegative_int, default=4_096)
    run.add_argument("--disable-cyclic-gc-in-hot-loop", action="store_true")

    inspect = subparsers.add_parser("inspect", help="inspect a result directory")
    inspect.add_argument("--result", required=True, type=Path)

    compare = subparsers.add_parser("compare", help="compare two result directories")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    return parser


def _performance_config(args: argparse.Namespace) -> PerformanceConfig:
    return PerformanceConfig(
        mode=args.mode,
        max_memory_mb=_optional_limit(args.max_memory_mb),
        max_events=_optional_limit(args.max_events),
        max_skip_ranges=_optional_limit(args.max_skip_ranges),
        max_diagnostics=_optional_limit(args.max_diagnostics),
        max_equity_points=_optional_limit(args.max_equity_points),
        progress_interval_bars=args.progress_interval_bars,
        retained_event_window=args.retained_event_window,
        equity_sampling=args.equity_sampling,
        equity_every_n_bars=args.equity_every_n_bars,
        max_base_history=_optional_limit(args.max_base_history),
        max_timeframe_history=_optional_limit(args.max_timeframe_history),
        disable_cyclic_gc_in_hot_loop=args.disable_cyclic_gc_in_hot_loop,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-data":
            config = load_config(args.config)
            validation = prepare_candle_source(config.data).validation
            print(json.dumps(validation.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.command == "run":
            config = load_config(args.config)
            performance = _performance_config(args)
            outcome = (
                run_backtest(config)
                if performance == PerformanceConfig()
                else run_backtest(config, performance)
            )
            print(
                json.dumps(
                    {
                        "run_id": outcome.run_id,
                        "bars_processed": outcome.bars_processed,
                        "output_directory": str(outcome.output_directory),
                        "restart_verified": outcome.restart_verified,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "inspect":
            snapshot = inspect_result(args.result)
            print(
                json.dumps(
                    {
                        "run_id": snapshot.run_id,
                        "directory": str(snapshot.directory),
                        "metrics": snapshot.metrics,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        print(json.dumps(compare_results(args.left, args.right), indent=2, sort_keys=True))
        return 0
    except ResourceLimitExceeded as exc:
        failure_directory = getattr(exc, "failure_directory", None)
        suffix = f"; failure_artifact={failure_directory}" if failure_directory else ""
        print(f"error: {exc}{suffix}", file=sys.stderr)
        return 3
    except (ConfigurationError, DataValidationError, ResultError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
