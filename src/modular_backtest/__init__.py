"""Application layer for deterministic modular backtests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._version import APPLICATION_VERSION

if TYPE_CHECKING:
    from .config import BacktestAppConfig, CheckpointConfig, load_config
    from .data import DataValidationError, ValidationSummary, stream_candles, validate_data
    from .recovery import ReplayCheckpoint, ReplayRecoveryError, ReplayRun, run_with_replay_restart
    from .runner import RunOutcome, run_backtest
    from .valuation import (
        MARK_VALUATION_MODEL_VERSION,
        RegisteredMarkPriceSource,
        VALUATION_POLICY_LAST,
        VALUATION_POLICY_MARK,
        open_registered_mark_price_source,
    )

__all__ = [
    "APPLICATION_VERSION",
    "BacktestAppConfig",
    "CheckpointConfig",
    "DataValidationError",
    "MARK_VALUATION_MODEL_VERSION",
    "RegisteredMarkPriceSource",
    "ReplayCheckpoint",
    "ReplayRecoveryError",
    "ReplayRun",
    "RunOutcome",
    "VALUATION_POLICY_LAST",
    "VALUATION_POLICY_MARK",
    "ValidationSummary",
    "load_config",
    "open_registered_mark_price_source",
    "run_backtest",
    "run_with_replay_restart",
    "stream_candles",
    "validate_data",
]

__version__ = APPLICATION_VERSION


def __getattr__(name: str) -> Any:
    if name in {"BacktestAppConfig", "CheckpointConfig", "load_config"}:
        from . import config as config_module

        value = getattr(config_module, name)
    elif name in {"DataValidationError", "ValidationSummary", "stream_candles", "validate_data"}:
        from . import data as data_module

        value = getattr(data_module, name)
    elif name in {
        "ReplayCheckpoint",
        "ReplayRecoveryError",
        "ReplayRun",
        "run_with_replay_restart",
    }:
        from . import recovery as recovery_module

        value = getattr(recovery_module, name)
    elif name in {"RunOutcome", "run_backtest"}:
        from . import runner as runner_module

        value = getattr(runner_module, name)
    elif name in {
        "MARK_VALUATION_MODEL_VERSION",
        "RegisteredMarkPriceSource",
        "VALUATION_POLICY_LAST",
        "VALUATION_POLICY_MARK",
        "open_registered_mark_price_source",
    }:
        from . import valuation as valuation_module

        value = getattr(valuation_module, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
