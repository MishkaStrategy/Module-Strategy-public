from __future__ import annotations

import inspect
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from .effective_config import (
    EffectiveConfigArtifactReference,
    PreparedBacktestConfiguration,
    verify_actual_backtest_configuration,
    verify_actual_performance_evidence,
    write_sidecar_for_job,
)
from .models import SpecificationError, TimeRange, freeze
from .output_manifest import OutputManifestError, write_output_manifest


class EngineContractError(RuntimeError):
    """Raised when the official Backtest Engine adapter contract is violated."""


@dataclass(frozen=True, slots=True)
class BacktestJob:
    run_id: str
    experiment_id: str
    stage_id: str
    split_id: str
    dataset_id: str
    scenario_id: str
    seed: int
    parameters: Mapping[str, Any]
    output_directory: Path
    stage_role: str = "unspecified"
    period: TimeRange | None = None
    repetition_index: int = 0
    metadata: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "experiment_id",
            "stage_id",
            "split_id",
            "dataset_id",
            "scenario_id",
            "stage_role",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise SpecificationError("seed must be an integer")
        if (
            not isinstance(self.repetition_index, int)
            or isinstance(self.repetition_index, bool)
            or self.repetition_index < 0
        ):
            raise SpecificationError("repetition_index must be a non-negative integer")
        if not isinstance(self.output_directory, Path):
            raise SpecificationError("output_directory must be a Path")
        if self.period is not None and not isinstance(self.period, TimeRange):
            raise SpecificationError("period must be a TimeRange when provided")
        if not isinstance(self.parameters, Mapping):
            raise SpecificationError("parameters must be a mapping")
        if not isinstance(self.metadata, Mapping):
            raise SpecificationError("metadata must be a mapping")
        object.__setattr__(self, "parameters", MappingProxyType(dict(freeze(self.parameters))))
        object.__setattr__(self, "metadata", MappingProxyType(dict(freeze(self.metadata))))

    def __reduce__(self) -> tuple[type[BacktestJob], tuple[Any, ...]]:
        return (
            type(self),
            (
                self.run_id,
                self.experiment_id,
                self.stage_id,
                self.split_id,
                self.dataset_id,
                self.scenario_id,
                self.seed,
                dict(self.parameters),
                self.output_directory,
                self.stage_role,
                self.period,
                self.repetition_index,
                dict(self.metadata),
            ),
        )


@dataclass(frozen=True, slots=True)
class BacktestRunReference:
    requested_run_id: str
    engine_run_id: str
    output_directory: Path
    bars_processed: int
    restart_verified: bool
    effective_config_artifact: EffectiveConfigArtifactReference | None = field(
        default=None,
        metadata={"canonical_omit_none": True},
    )

    def __post_init__(self) -> None:
        for name in ("requested_run_id", "engine_run_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        if not isinstance(self.output_directory, Path):
            raise SpecificationError("output_directory must be a Path")
        if (
            not isinstance(self.bars_processed, int)
            or isinstance(self.bars_processed, bool)
            or self.bars_processed < 0
        ):
            raise SpecificationError("bars_processed must be a non-negative integer")
        if not isinstance(self.restart_verified, bool):
            raise SpecificationError("restart_verified must be a boolean")
        if self.effective_config_artifact is not None and not isinstance(
            self.effective_config_artifact,
            EffectiveConfigArtifactReference,
        ):
            raise SpecificationError("effective_config_artifact must be a typed reference")


class BacktestConfigFactory(Protocol):
    def __call__(self, job: BacktestJob) -> Any: ...


class BacktestRunFunction(Protocol):
    def __call__(self, config: Any) -> Any: ...


class ModularBacktestEngineAdapter:
    """Thin adapter to the existing modular_backtest application contract.

    The adapter does not simulate bars, orders, fills, positions or metrics. It only
    builds an official application configuration and delegates to run_backtest().
    """

    def __init__(
        self,
        config_factory: BacktestConfigFactory,
        run_function: BacktestRunFunction | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._run_function = run_function

    def execute(self, job: BacktestJob) -> BacktestRunReference:
        run_function = self._run_function
        if run_function is None:
            try:
                from modular_backtest import run_backtest
            except ImportError as exc:
                raise EngineContractError(
                    "modular_backtest is required from the authoritative Backtest Engine branch"
                ) from exc
            run_function = run_backtest
        configured = self._config_factory(job)
        prepared: PreparedBacktestConfiguration | None = None
        if isinstance(configured, PreparedBacktestConfiguration):
            prepared = configured
            _validate_prepared_context(job, prepared)
            try:
                verify_actual_backtest_configuration(prepared)
            except SpecificationError as exc:
                raise EngineContractError(
                    "effective Backtest configuration differs from its actual typed config"
                ) from exc
            engine_config = prepared.config
        else:
            engine_config = configured
        registered_options_scope: AbstractContextManager[None]
        if prepared is None:
            registered_options_scope = nullcontext()
        else:
            source = prepared.effective_configuration["data_source"]
            if source["source_kind"] == "registered":
                try:
                    from modular_backtest.streaming_data import frozen_registered_data_options
                except ImportError as exc:
                    raise EngineContractError(
                        "effective registered data options cannot be frozen for execution"
                    ) from exc
                registered_options_scope = frozen_registered_data_options(
                    source["registered_mode"],
                    source["stream_batch_size"],
                )
            else:
                registered_options_scope = nullcontext()
        with registered_options_scope:
            if prepared is None:
                outcome = run_function(engine_config)
            else:
                try:
                    inspect.signature(run_function).bind(
                        engine_config,
                        performance=prepared.performance_config,
                    )
                except (TypeError, ValueError) as exc:
                    raise EngineContractError(
                        "effective config callback must accept the frozen PerformanceConfig"
                    ) from exc
                outcome = cast(Any, run_function)(
                    engine_config,
                    performance=prepared.performance_config,
                )
            if prepared is not None:
                try:
                    verify_actual_backtest_configuration(prepared)
                except SpecificationError as exc:
                    raise EngineContractError(
                        "effective Backtest configuration changed during engine execution"
                    ) from exc
        for name in ("run_id", "output_directory", "bars_processed", "restart_verified"):
            if not hasattr(outcome, name):
                raise EngineContractError(f"Backtest Engine outcome is missing {name}")

        engine_run_id = outcome.run_id
        if not isinstance(engine_run_id, str) or not engine_run_id.strip():
            raise EngineContractError("Backtest Engine outcome run_id must be non-empty")
        output_value = outcome.output_directory
        if not isinstance(output_value, str | Path):
            raise EngineContractError(
                "Backtest Engine outcome output_directory must be a path string or Path"
            )
        bars_processed = outcome.bars_processed
        if (
            not isinstance(bars_processed, int)
            or isinstance(bars_processed, bool)
            or bars_processed < 0
        ):
            raise EngineContractError(
                "Backtest Engine outcome bars_processed must be a non-negative integer"
            )
        restart_verified = outcome.restart_verified
        if not isinstance(restart_verified, bool):
            raise EngineContractError("Backtest Engine outcome restart_verified must be a boolean")

        output = Path(output_value)
        if not output.is_absolute():
            output = job.output_directory / output
        if prepared is not None:
            try:
                verify_actual_performance_evidence(
                    prepared,
                    output,
                    engine_run_id=engine_run_id,
                )
            except SpecificationError as exc:
                raise EngineContractError(
                    "Backtest producer PerformanceConfig evidence differs from projection"
                ) from exc
        effective_reference: EffectiveConfigArtifactReference | None = None
        if prepared is not None:
            try:
                effective_reference = write_sidecar_for_job(
                    job.output_directory,
                    prepared,
                    engine_run_id=engine_run_id,
                    result_output_directory=output,
                )
            except (OSError, SpecificationError) as exc:
                raise EngineContractError(
                    "effective Backtest configuration could not be bound to the run"
                ) from exc
        reference = BacktestRunReference(
            requested_run_id=job.run_id,
            engine_run_id=engine_run_id,
            output_directory=output,
            bars_processed=bars_processed,
            restart_verified=restart_verified,
            effective_config_artifact=effective_reference,
        )
        try:
            write_output_manifest(reference)
        except (OutputManifestError, OSError) as exc:
            raise EngineContractError(
                "Backtest Engine output could not be bound to a verified manifest"
            ) from exc
        return reference


def _validate_prepared_context(
    job: BacktestJob,
    prepared: PreparedBacktestConfiguration,
) -> None:
    metadata = job.metadata
    configuration_sha256 = metadata.get("configuration_sha256")
    if not isinstance(configuration_sha256, str):
        raise EngineContractError("v2 effective config requires the planned configuration hash")
    expected = {
        "run_id": job.run_id,
        "experiment_id": job.experiment_id,
        "stage_id": job.stage_id,
        "stage_role": job.stage_role,
        "split_id": job.split_id,
        "dataset_id": job.dataset_id,
        "scenario_id": job.scenario_id,
        "seed": job.seed,
        "repetition_index": job.repetition_index,
        "configuration_sha256": configuration_sha256,
    }
    if any(prepared.context.get(key) != value for key, value in expected.items()):
        raise EngineContractError("effective Backtest configuration context differs from job")
    if job.period is not None:
        start = (
            job.period.start.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        end = (
            job.period.end.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        if (
            prepared.context.get("period_start") != start
            or prepared.context.get("period_end") != end
        ):
            raise EngineContractError("effective Backtest period differs from job")
