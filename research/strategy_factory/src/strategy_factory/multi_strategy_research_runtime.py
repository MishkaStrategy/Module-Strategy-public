"""Owner-native Research runtime for the governed MS5A multi-strategy workstream.

This module deliberately stops at the pre-holdout composition.  It reuses the
registered Market Data owner, canonical funding valuation source, canonical
Research Runner lineage adapter, and the trusted runtime-module renderer from
``OfficialBacktestConfigFactory``.  It never resolves ``dataset.uri`` and does
not expose a HOLDOUT/final-test handler or authority surface.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from modular_research_runner.effective_config import EFFECTIVE_CONFIG_CONTRACT

from .backtest_config import BacktestEnginePolicy, OfficialBacktestConfigFactory
from .canonical_owner_research import CanonicalOwnerResearchRunnerAdapter
from .errors import CompatibilityError, ExternalComponentUnavailableError
from .multi_strategy_owner import (
    MS5A_COST_STRESS_MULTIPLIER,
    MS5A_COST_STRESS_SCENARIO_ID,
    MS5A_DATASET_ID,
    MS5A_DATASET_VERSION,
    MS5A_INITIAL_CAPITAL,
    MS5A_RISK_FRACTION,
    OwnerState,
)
from .multi_strategy_runtime_closure import ms5a_instrument_policy
from .multi_strategy_search_surface import MS5ASearchSurface
from .run import StrategyFactoryRun

RESULTS_PRODUCER_VERSION = "strategy-factory-ms5a-search-v1"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    factory_state_root: Path
    research_state_path: Path
    research_artifact_root: Path
    research_output_root: Path
    results_package_root: Path
    results_registry_path: Path


_RETRY_POLICY = {
    "max_attempts": 2,
    "initial_backoff_seconds": 1.0,
    "backoff_multiplier": 2.0,
    "maximum_backoff_seconds": 30.0,
    "retryable_categories": ("TRANSIENT", "RESOURCE"),
}
_RESUME_POLICY = {
    "lease_seconds": 900,
    "heartbeat_seconds": 60,
    "recover_expired_leases": True,
    "verify_success_artifacts": True,
}
_ALLOWED_PERFORMANCE_MODES = frozenset({"canonical_audit", "research_fast"})
_OWNER_WORKFLOW_TIMEOUT_SECONDS = 1560 * 60
_OWNER_WORKFLOW_CLEANUP_MARGIN_SECONDS = 60 * 60
_OWNER_HARD_WATCHDOG_SECONDS = (
    _OWNER_WORKFLOW_TIMEOUT_SECONDS - _OWNER_WORKFLOW_CLEANUP_MARGIN_SECONDS
)
_OWNER_RESEARCH_RETURN_MARGIN_SECONDS = 60 * 60
_OWNER_EXECUTION_WINDOW_SECONDS = (
    _OWNER_HARD_WATCHDOG_SECONDS - _OWNER_RESEARCH_RETURN_MARGIN_SECONDS
)


def _execution_config_for_performance_mode(config: Any, performance_mode: str) -> Any:
    """Keep replay audit fail-closed while ordinary MS5A research stays bounded."""

    if performance_mode != "research_fast":
        return config
    checkpoint = getattr(config, "checkpoint", None)
    if checkpoint is None or getattr(checkpoint, "mode", None) != "replay":
        return config
    try:
        disabled_checkpoint = replace(
            checkpoint,
            mode="disabled",
            restart_at_bar=None,
        )
        return replace(config, checkpoint=disabled_checkpoint)
    except (TypeError, ValueError) as exc:
        raise CompatibilityError("MS5A research checkpoint contract is incompatible") from exc


def _ms5a_execution_window_seconds(run: Any) -> int:
    """Bound Research below the parent watchdog with time to return terminal evidence."""

    request = getattr(run, "request", None)
    budget = getattr(request, "budget", None)
    configured = getattr(budget, "maximum_duration_seconds", None)
    if configured is None:
        return _OWNER_EXECUTION_WINDOW_SECONDS
    if not isinstance(configured, int) or isinstance(configured, bool) or configured < 1:
        raise CompatibilityError("MS5A maximum_duration_seconds is invalid")
    return min(configured, _OWNER_EXECUTION_WINDOW_SECONDS)


class _OwnerFundingBacktestRunner:
    """Owner-funded callback with fail-closed fallback and role-aware opt-in."""

    def __init__(
        self,
        *,
        run_backtest: Any,
        performance_config: Any,
        funding_source: Any,
        execution_window_seconds: int = _OWNER_EXECUTION_WINDOW_SECONDS,
        expected_effective_performance_mode: str | None = None,
    ) -> None:
        if not callable(run_backtest):
            raise TypeError("run_backtest must be callable")
        if not callable(performance_config):
            raise TypeError("performance_config must be callable")
        if (
            not isinstance(execution_window_seconds, int)
            or isinstance(execution_window_seconds, bool)
            or execution_window_seconds < 1
        ):
            raise ValueError("execution_window_seconds must be a positive integer")
        if expected_effective_performance_mode not in {
            None,
            "canonical_audit",
            "research_fast",
        }:
            raise ValueError("expected_effective_performance_mode is unsupported")
        self._run_backtest = run_backtest
        self._performance_config = performance_config
        self._funding_source = funding_source
        self._expected_effective_performance_mode = expected_effective_performance_mode
        # One absolute deadline is shared by every backtest invoked through this
        # owner runtime. It expires before the parent hard watchdog so Research
        # Runner can unwind and persist a terminal result instead of racing the
        # process-group kill. Sequential experiments cannot reset this window.
        self._deadline_at = time.monotonic() + execution_window_seconds

    def __call__(self, config: Any) -> Any:
        """Preserve the legacy one-argument injected callback contract safely."""

        return self.run_with_performance_mode(
            config,
            performance_mode="canonical_audit",
        )

    def run_with_performance_mode(
        self,
        config: Any,
        *,
        performance_mode: str,
        performance: Any | None = None,
    ) -> Any:
        if performance_mode not in _ALLOWED_PERFORMANCE_MODES:
            raise CompatibilityError("unsupported MS5A performance mode")
        if (
            self._expected_effective_performance_mode is not None
            and performance_mode != self._expected_effective_performance_mode
        ):
            raise CompatibilityError(
                "effective config performance mode differs from Backtest callback"
            )
        execution_config = _execution_config_for_performance_mode(config, performance_mode)
        if performance is None:
            performance = self._performance_config(mode=performance_mode)
        if getattr(performance, "mode", None) != performance_mode:
            raise CompatibilityError("effective config performance mode differs from callback")
        deadline_scope = getattr(performance, "execution_deadline_scope", None)
        if callable(deadline_scope):
            scope = deadline_scope(self._deadline_at)
        elif type(performance).__module__.startswith("modular_backtest"):
            # A genuine owner Backtest Engine without the deadline contract would
            # silently reintroduce the parent-workflow external-kill failure mode.
            raise CompatibilityError(
                "Backtest Engine performance config lacks execution deadline contract"
            )
        else:
            # Preserve the injected callback contract for isolated test doubles.
            # Production PerformanceConfig originates from modular_backtest and is
            # required to expose execution_deadline_scope above.
            scope = nullcontext()
        with scope:
            return self._run_backtest(
                execution_config,
                performance,
                funding_source=self._funding_source,
            )


def ms5a_backtest_policy() -> BacktestEnginePolicy:
    """Return the unchanged canonical execution/cost/risk/instrument contract."""

    return BacktestEnginePolicy(
        execution={"slippage_bps": 2.5, "stop_slippage_bps": 2.5},
        fees={"maker_rate": 0.0002, "taker_rate": 0.00055},
        risk={
            "default_risk_fraction": float(MS5A_RISK_FRACTION),
            "max_risk_fraction": float(MS5A_RISK_FRACTION),
        },
        instrument=ms5a_instrument_policy(),
        gap_policy="reject",
        checkpoint_mode="replay",
        end_of_test_policy="force_close",
    )


def _scenario_cost_multiplier(job: Any) -> float:
    raw_metadata = getattr(job, "metadata", {})
    if not isinstance(raw_metadata, Mapping):
        raise CompatibilityError("MS5A Research scenario metadata is malformed")
    scenario_id = getattr(job, "scenario_id", "base")
    scenario_category = raw_metadata.get("scenario_category", "base")
    scenario_overrides = raw_metadata.get("scenario_overrides", {})
    if (
        not isinstance(scenario_id, str)
        or not isinstance(scenario_category, str)
        or not isinstance(scenario_overrides, Mapping)
    ):
        raise CompatibilityError("MS5A Research scenario metadata is malformed")
    if scenario_category == "base":
        if scenario_id != "base" or scenario_overrides:
            raise CompatibilityError("unsupported MS5A scenario override")
        return 1.0
    if scenario_category == "cost":
        expected = {"execution_cost_multiplier": MS5A_COST_STRESS_MULTIPLIER}
        if scenario_id != MS5A_COST_STRESS_SCENARIO_ID or dict(scenario_overrides) != expected:
            raise CompatibilityError("unsupported MS5A scenario override")
        return float(MS5A_COST_STRESS_MULTIPLIER)
    raise CompatibilityError("unsupported MS5A scenario category")


class MS5ABacktestConfigFactory:
    """Render trusted dynamic modules over the registered canonical data owner."""

    def __init__(
        self,
        *,
        run: StrategyFactoryRun,
        surface: MS5ASearchSurface,
        market_data_root: str | Path,
        allowed_output_root: str | Path,
        effective_config_contract: str | None = None,
        execution_performance_mode: str | None = None,
    ) -> None:
        if not isinstance(run, StrategyFactoryRun):
            raise TypeError("run must be StrategyFactoryRun")
        if not isinstance(surface, MS5ASearchSurface):
            raise TypeError("surface must be MS5ASearchSurface")
        if surface.source_commit_sha != run.commit_sha:
            raise CompatibilityError("MS5A runtime surface differs from exact run commit")
        if run.request.dataset.dataset_id != MS5A_DATASET_ID:
            raise CompatibilityError("MS5A request is not bound to the canonical dataset owner")
        if run.request.initial_capital != MS5A_INITIAL_CAPITAL:
            raise CompatibilityError("MS5A initial capital differs from canonical contract")
        if run.request.research_risk_fraction != MS5A_RISK_FRACTION:
            raise CompatibilityError("MS5A research risk differs from canonical contract")
        self._run = run
        self._market_data_root = Path(market_data_root)
        if execution_performance_mode not in {None, "canonical_audit", "research_fast"}:
            raise CompatibilityError("execution_performance_mode is unsupported")
        self._execution_performance_mode = execution_performance_mode
        if effective_config_contract is not None:
            if execution_performance_mode is None:
                raise CompatibilityError("effective config requires an explicit performance mode")
        self._policy = ms5a_backtest_policy()
        self._renderer = OfficialBacktestConfigFactory(
            request=run.request,
            policy=self._policy,
            trusted_modules=surface.trusted_runtime_modules,
            allowed_output_root=allowed_output_root,
            effective_config_contract=effective_config_contract,
            execution_performance_mode=execution_performance_mode,
        )

    def __call__(self, job: Any) -> Any:
        try:
            from modular_backtest import BacktestAppConfig, CheckpointConfig
            from modular_backtest.config import DataSourceConfig, OutputConfig
        except ImportError as exc:
            raise ExternalComponentUnavailableError(
                "Backtest Engine application config is unavailable"
            ) from exc

        if getattr(job, "dataset_id", None) != MS5A_DATASET_ID:
            raise CompatibilityError(
                "Research job is not bound to the canonical registered dataset"
            )
        parameters = getattr(job, "parameters", None)
        if not isinstance(parameters, Mapping):
            raise CompatibilityError("Research Runner job parameters must be a mapping")
        output_directory = self._renderer._job_output_directory(job)
        start, end = self._renderer._job_period(job)
        modules_payload = self._renderer._module_configuration(parameters)
        modules_path = self._renderer._write_modules_configuration(
            output_directory,
            modules_payload,
        )
        multiplier = _scenario_cost_multiplier(job)
        requested_timeframes = tuple(
            timeframe
            for timeframe in self._run.request.allowed_timeframes
            if timeframe != self._run.request.dataset.base_timeframe
        )
        execution = {
            name: float(value) * multiplier for name, value in self._policy.execution.items()
        }
        fees = {name: float(value) * multiplier for name, value in self._policy.fees.items()}
        config = BacktestAppConfig(
            data=DataSourceConfig(
                symbol=self._run.request.symbol,
                storage_root=self._market_data_root,
                dataset_id=MS5A_DATASET_ID,
                dataset_version=MS5A_DATASET_VERSION,
                registered_mode="streaming",
                start=start,
                end=end,
                gap_policy=self._policy.gap_policy,
            ),
            modules=modules_path,
            initial_capital=float(self._run.request.initial_capital),
            requested_timeframes=requested_timeframes,
            execution=execution,
            fees=fees,
            risk=self._policy.risk,
            instrument=self._policy.instrument,
            output=OutputConfig(directory=output_directory),
            seed=job.seed,
            checkpoint=CheckpointConfig(mode=self._policy.checkpoint_mode),
            end_of_test_policy=self._policy.end_of_test_policy,
        )
        effective_config_contract = getattr(self._renderer, "_effective_config_contract", None)
        if effective_config_contract is None:
            return config
        if effective_config_contract != EFFECTIVE_CONFIG_CONTRACT:
            raise CompatibilityError("effective config contract is unsupported")
        if self._execution_performance_mode is None:
            raise CompatibilityError(
                "effective config requires the actual execution performance mode"
            )
        config = _execution_config_for_performance_mode(config, self._execution_performance_mode)
        try:
            from modular_backtest.scalability import PerformanceConfig

            performance = PerformanceConfig(mode=self._execution_performance_mode)
        except (ImportError, TypeError, ValueError) as exc:
            raise CompatibilityError(
                "effective config requires Backtest PerformanceConfig"
            ) from exc
        return self._renderer._prepared_effective_configuration(
            job,
            config,
            modules_payload=modules_payload,
            modules_path=modules_path,
            performance_config=performance,
        )


def build_ms5a_research_runtime(
    *,
    run: StrategyFactoryRun,
    owner: OwnerState,
    surface: MS5ASearchSurface,
    paths: RuntimePaths,
    market_data_root: str | Path,
    git_provenance: Any,
    environment_manifest: Any,
    effective_config_contract: str | None = None,
) -> tuple[Any, Any, CanonicalOwnerResearchRunnerAdapter, Any, Any]:
    """Build the genuine owner-backed Research Runner for one exact MS5A run."""

    try:
        import modular_backtest
        from modular_backtest.scalability import PerformanceConfig
        from modular_research_runner import (
            AtomicJsonArtifactStore,
            LocalResearchScheduler,
            ModularBacktestEngineAdapter,
            ResearchPlanBuilder,
            ResearchRunner,
            ResumePolicy,
            RetryPolicy,
            SqliteExperimentRepository,
        )
    except ImportError as exc:
        raise ExternalComponentUnavailableError(
            "Research Runner or Backtest Engine owner package is unavailable"
        ) from exc

    repository = SqliteExperimentRepository(paths.research_state_path)
    effective_performance_mode = "research_fast"
    config_factory = MS5ABacktestConfigFactory(
        run=run,
        surface=surface,
        market_data_root=market_data_root,
        allowed_output_root=paths.research_output_root,
        effective_config_contract=effective_config_contract,
        execution_performance_mode=(
            effective_performance_mode if effective_config_contract is not None else None
        ),
    )
    run_function = _OwnerFundingBacktestRunner(
        run_backtest=modular_backtest.run_backtest,
        performance_config=PerformanceConfig,
        funding_source=owner.funding_source,
        execution_window_seconds=_ms5a_execution_window_seconds(run),
        expected_effective_performance_mode=(
            effective_performance_mode if effective_config_contract is not None else None
        ),
    )

    engine = ModularBacktestEngineAdapter(
        config_factory,
        partial(
            run_function.run_with_performance_mode,
            performance_mode=effective_performance_mode,
        ),
    )
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=engine,
        retry_policy=RetryPolicy.from_mapping(_RETRY_POLICY),
        resume_policy=ResumePolicy.from_mapping(_RESUME_POLICY),
        maximum_workers=1,
    )
    runner = ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(paths.research_artifact_root),
        plan_builder=ResearchPlanBuilder(paths.research_output_root),
        scheduler=scheduler,
        backtest_engine_version=f"modular-backtest-{modular_backtest.APPLICATION_VERSION}",
        metric_schema_version="modular-core.metrics-0.4.0",
        module_api_version="1.0",
    )
    adapter = CanonicalOwnerResearchRunnerAdapter(
        output_root=paths.research_output_root,
        dataset_file_size=owner.dataset_file_size,
        runtime_closure_blueprint=surface.runtime_closure.blueprint,
    )
    return runner, repository, adapter, git_provenance, environment_manifest


__all__ = [
    "MS5ABacktestConfigFactory",
    "RESULTS_PRODUCER_VERSION",
    "RuntimePaths",
    "build_ms5a_research_runtime",
    "ms5a_backtest_policy",
]
