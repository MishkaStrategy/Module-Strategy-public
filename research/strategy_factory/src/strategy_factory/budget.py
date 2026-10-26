"""Deterministic budget accounting with stage-level fail-closed limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic_ns

from .contracts import FactoryStage, ResourceBudget
from .errors import BudgetExhaustedError, ContractError


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    experiments_reserved: int
    architectures_generated: int
    finalists_selected: int
    stage_experiments: dict[str, int]
    elapsed_seconds: int


@dataclass(slots=True)
class BudgetLedger:
    """In-memory accounting state persisted through StrategyFactoryCheckpoint."""

    limits: ResourceBudget
    experiments_reserved: int = 0
    architectures_generated: int = 0
    finalists_selected: int = 0
    stage_experiments: dict[str, int] = field(default_factory=dict)
    _started_ns: int = field(default_factory=monotonic_ns, repr=False)

    def reserve_experiments(self, stage: FactoryStage, count: int) -> None:
        """Atomically reserve experiment capacity or leave the ledger unchanged."""

        _positive(count, "experiment count")
        stage_key = stage.value
        next_total = self.experiments_reserved + count
        next_stage = self.stage_experiments.get(stage_key, 0) + count
        if next_total > self.limits.maximum_experiments:
            raise BudgetExhaustedError("maximum experiment budget exhausted")
        stage_limit = self.limits.stage_experiment_limits.get(stage_key)
        if stage_limit is not None and next_stage > stage_limit:
            raise BudgetExhaustedError(f"stage budget exhausted: {stage_key}")
        self.experiments_reserved = next_total
        self.stage_experiments[stage_key] = next_stage

    def record_architectures(self, count: int) -> None:
        _positive(count, "architecture count", allow_zero=True)
        next_value = self.architectures_generated + count
        if next_value > self.limits.maximum_architectures:
            raise BudgetExhaustedError("maximum architecture budget exhausted")
        self.architectures_generated = next_value

    def record_finalists(self, count: int) -> None:
        _positive(count, "finalist count", allow_zero=True)
        if count > self.limits.maximum_finalists:
            raise BudgetExhaustedError("maximum finalist budget exhausted")
        self.finalists_selected = count

    def ensure_time_available(self) -> None:
        if self.elapsed_seconds >= self.limits.maximum_duration_seconds:
            raise BudgetExhaustedError("maximum duration budget exhausted")

    @property
    def elapsed_seconds(self) -> int:
        return max(0, (monotonic_ns() - self._started_ns) // 1_000_000_000)

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            experiments_reserved=self.experiments_reserved,
            architectures_generated=self.architectures_generated,
            finalists_selected=self.finalists_selected,
            stage_experiments=dict(sorted(self.stage_experiments.items())),
            elapsed_seconds=self.elapsed_seconds,
        )

    def restore(self, state: Mapping[str, object]) -> None:
        """Restore validated counters; wall time intentionally restarts after recovery."""

        experiments = _read_int(state, "experiments_reserved")
        architectures = _read_int(state, "architectures_generated")
        finalists = _read_int(state, "finalists_selected")
        raw_stages = state.get("stage_experiments", {})
        if not isinstance(raw_stages, Mapping):
            raise ContractError("stage_experiments must be an object")
        stages: dict[str, int] = {}
        for key, value in raw_stages.items():
            if not isinstance(key, str) or key not in {item.value for item in FactoryStage}:
                raise ContractError("invalid restored budget stage")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError("invalid restored stage budget")
            stages[key] = value
        if experiments > self.limits.maximum_experiments:
            raise ContractError("restored experiment budget exceeds limits")
        if architectures > self.limits.maximum_architectures:
            raise ContractError("restored architecture budget exceeds limits")
        if finalists > self.limits.maximum_finalists:
            raise ContractError("restored finalist budget exceeds limits")
        if sum(stages.values()) != experiments:
            raise ContractError("restored stage budget does not match total")
        for key, count in stages.items():
            limit = self.limits.stage_experiment_limits.get(key)
            if limit is not None and count > limit:
                raise ContractError("restored stage budget exceeds limits")
        self.experiments_reserved = experiments
        self.architectures_generated = architectures
        self.finalists_selected = finalists
        self.stage_experiments = stages
        self._started_ns = monotonic_ns()


def _positive(value: int, name: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")


def _read_int(state: Mapping[str, object], key: str) -> int:
    value = state.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"invalid restored budget counter: {key}")
    return value
