from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .errors import ValidationError
from .models import (
    CandidateRole,
    ConfigurationRecord,
    DatasetRecord,
    ExperimentRecord,
    ObjectiveDirection,
    RegistrationPackage,
    RunStatus,
    finite_nonnegative_number,
    nonblank,
    nonnegative_integer,
    utc,
    validate_id,
)

_REQUIRED_FILES = ("run.json", "metrics.json", "journal.json", "trades.csv", "equity.csv")
_OPTIONAL_FILES = ("events.jsonl", "effective-backtest-config.json", "application-run.json")
_MAX_JSON_BYTES = 64 * 1024 * 1024

_METRIC_CONTRACT: Mapping[str, tuple[str, int | None]] = {
    "initial_equity": ("currency", 8),
    "final_equity": ("currency", 8),
    "net_profit": ("currency", 8),
    "return_pct": ("percent", 8),
    "trades": ("count", 0),
    "wins": ("count", 0),
    "losses": ("count", 0),
    "breakevens": ("count", 0),
    "win_rate": ("percent", 8),
    "gross_profit": ("currency", 8),
    "gross_loss": ("currency", 8),
    "profit_factor": ("ratio", 8),
    "expectancy": ("currency_per_trade", 8),
    "average_win": ("currency", 8),
    "average_loss": ("currency", 8),
    "payoff_ratio": ("ratio", 8),
    "max_drawdown": ("currency", 8),
    "max_drawdown_pct": ("percent", 8),
    "max_consecutive_wins": ("count", 0),
    "max_consecutive_losses": ("count", 0),
    "average_r_multiple": ("R", 8),
    "average_holding_bars": ("bars", 8),
    "exposure_pct": ("percent", 8),
    "total_fees": ("currency", 8),
}

_METRIC_FORMULAS: Mapping[str, str] = {
    "initial_equity": "initial equity supplied to calculate_metrics",
    "final_equity": "last equity point, or initial_equity when no equity points exist",
    "net_profit": "final_equity - initial_equity",
    "return_pct": "100 * net_profit / initial_equity when initial_equity is non-zero, else 0",
    "trades": "count(trades)",
    "wins": "count(trade.net_pnl > 0)",
    "losses": "count(trade.net_pnl < 0)",
    "breakevens": "trades - wins - losses",
    "win_rate": "100 * wins / trades when trades > 0, else 0",
    "gross_profit": "sum(positive trade.net_pnl)",
    "gross_loss": "abs(sum(negative trade.net_pnl))",
    "profit_factor": "gross_profit / gross_loss when gross_loss > 0; producer sentinel otherwise",
    "expectancy": "sum(trade.net_pnl) / trades when trades > 0, else 0",
    "average_win": "gross_profit / wins when wins > 0, else 0",
    "average_loss": "-gross_loss / losses when losses > 0, else 0",
    "payoff_ratio": "average_win / abs(average_loss) when average_loss != 0; producer sentinel otherwise",
    "max_drawdown": "maximum running equity peak minus current equity",
    "max_drawdown_pct": "100 * maximum drawdown / running equity peak",
    "max_consecutive_wins": "maximum consecutive strictly positive trade.net_pnl values",
    "max_consecutive_losses": "maximum consecutive strictly negative trade.net_pnl values",
    "average_r_multiple": "mean(non-null trade.r_multiple), else null",
    "average_holding_bars": "mean(non-null trade.holding_bars), else null",
    "exposure_pct": "100 * equity points with position_open / equity point count",
    "total_fees": "sum(trade.fees)",
}

_METRIC_DIRECTIONS: Mapping[str, ObjectiveDirection] = {
    "return_pct": ObjectiveDirection.MAXIMIZE,
    "profit_factor": ObjectiveDirection.MAXIMIZE,
    "expectancy": ObjectiveDirection.MAXIMIZE,
    "max_drawdown": ObjectiveDirection.MINIMIZE,
    "max_drawdown_pct": ObjectiveDirection.MINIMIZE,
    "total_fees": ObjectiveDirection.MINIMIZE,
}

_FILE_SCHEMAS: Mapping[str, tuple[str, str, str]] = {
    "run.json": ("application/json", "modular-core.run-export", "0.4.0"),
    "metrics.json": ("application/json", "modular-core.metrics", "0.4.0"),
    "journal.json": ("application/json", "modular-core.journal", "0.4.0"),
    "trades.csv": ("text/csv", "modular-core.trades", "0.4.0"),
    "equity.csv": ("text/csv", "modular-core.equity", "0.4.0"),
    "events.jsonl": ("application/x-ndjson", "modular-core.events", "0.4.0"),
    "effective-backtest-config.json": (
        "application/json",
        "modular-backtest.effective-backtest-config",
        "3",
    ),
    "application-run.json": (
        "application/json",
        "modular-backtest.application-run",
        "1",
    ),
}


@dataclass(frozen=True, slots=True)
class CoreRunContext:
    experiment: ExperimentRecord
    configuration: ConfigurationRecord
    datasets: tuple[DatasetRecord, ...]
    stage_id: str
    split_id: str
    scenario_id: str
    seed: int
    worker_id: str
    machine_id: str
    run_status: RunStatus
    observed_completed_at: datetime
    duration_seconds: float | None
    role: CandidateRole
    role_reason: str
    metric_split: str
    metric_period: str
    metric_scenario: str

    def __post_init__(self) -> None:
        if not isinstance(self.experiment, ExperimentRecord):
            raise ValidationError("experiment must be an ExperimentRecord")
        if not isinstance(self.configuration, ConfigurationRecord):
            raise ValidationError("configuration must be a ConfigurationRecord")
        if self.configuration.strategy_id != self.experiment.strategy_id:
            raise ValidationError("configuration strategy does not match experiment")
        if (
            not isinstance(self.datasets, tuple)
            or not self.datasets
            or not all(isinstance(item, DatasetRecord) for item in self.datasets)
        ):
            raise ValidationError("datasets must be a non-empty tuple of DatasetRecord")
        dataset_ids = tuple(item.dataset_id for item in self.datasets)
        if len(set(dataset_ids)) != len(dataset_ids):
            raise ValidationError("datasets must be unique")
        object.__setattr__(
            self, "datasets", tuple(sorted(self.datasets, key=lambda item: item.dataset_id))
        )
        for name in ("stage_id", "split_id", "scenario_id", "worker_id", "machine_id"):
            validate_id(name, getattr(self, name))
        nonnegative_integer("seed", self.seed)
        if not isinstance(self.run_status, RunStatus):
            raise ValidationError("run_status must be a RunStatus")
        observed = utc(self.observed_completed_at, "observed_completed_at")
        if observed < self.experiment.started_at:
            raise ValidationError("observed_completed_at precedes experiment start")
        object.__setattr__(self, "observed_completed_at", observed)
        if self.duration_seconds is not None:
            finite_nonnegative_number("duration_seconds", self.duration_seconds)
        if not isinstance(self.role, CandidateRole):
            raise ValidationError("role must be a CandidateRole")
        for name in ("role_reason", "metric_split", "metric_period", "metric_scenario"):
            nonblank(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class CoreImportResult:
    package: RegistrationPackage
    source_inventory: Mapping[str, Mapping[str, Any]]
