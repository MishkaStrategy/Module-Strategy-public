from __future__ import annotations

import errno
import json
import os
import random
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from modular_core.engine import BacktestEngine, BacktestResult
from modular_core.manifest import ModuleRegistry
from modular_core.timeframes import TimeframeSpec

from ._version import APPLICATION_VERSION
from .config import BacktestAppConfig
from .data import ValidationSummary, load_validated_candles
from .funding import RegisteredFundingSource
from .funding_application import (
    application_run_id,
    collect_funding_runtime_evidence,
    funding_application_metadata,
    funding_execution_factory,
)
from .metrics import calculate_extended_metrics
from .provenance import SourceProvenance, build_source_provenance
from .recovery import (
    ReplayRun,
    run_with_replay_restart,
    run_with_runtime_snapshot_restart,
)
from .registered_recovery import (
    RegisteredStreamingReplayRun,
    run_with_registered_streaming_replay_restart,
)
from .results import ResultError, verify_artifact_manifest, write_artifact_manifest
from .scalability import (
    PerformanceConfig,
    ResourceLimitExceeded,
    ScalableBacktestEngine,
    ScalableJournal,
)
from .scalability.engine import PreparedCandleStream
from .streaming_data import (
    PreparedCandleSource,
    current_frozen_registered_data_options,
    prepare_candle_source,
)
from .valuation import (
    ExecutionFactory,
    RegisteredMarkPriceSource,
    VALUATION_POLICY_LAST,
    VALUATION_POLICY_MARK,
    validate_valuation_inputs,
    valuation_application_metadata,
    valuation_application_run_id,
    valuation_execution_factory,
    valuation_runtime_evidence,
)


class ResultPublicationError(ResultError):
    """Raised when a complete deterministic result cannot be published safely."""


@dataclass(frozen=True, slots=True)
class RunOutcome:
    output_directory: Path
    validation: ValidationSummary
    run_id: str
    bars_processed: int
    restart_verified: bool = False
    application_run_id: str | None = None


RecoveryRun = ReplayRun | RegisteredStreamingReplayRun

_RATIO_BY_TIMEFRAME = {"15m": 3, "1h": 12, "4h": 48, "1d": 288}
_REGISTERED_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_REGISTERED_BATCH_SIZE_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"
_REGISTERED_MODES = frozenset({"legacy_materialized", "streaming"})


def _timeframe_specs(
    values: tuple[str, ...],
    max_history: int | None = None,
) -> tuple[TimeframeSpec, ...]:
    return tuple(
        TimeframeSpec(
            ratio=_RATIO_BY_TIMEFRAME[value],
            output_timeframe=value,
            gap_safe=True,
            align_to_clock=True,
            alignment_timezone="UTC",
            max_history=max_history,
        )
        for value in values
    )


def _restart_index(config: BacktestAppConfig, bars: int) -> int | None:
    if config.checkpoint.mode == "disabled":
        return None
    if bars < 2:
        raise ValueError("checkpoint replay requires at least two candles")
    if config.checkpoint.restart_at_bar is not None:
        index = config.checkpoint.restart_at_bar
    else:
        index = max(0, bars // 2 - 1)
    if index >= bars - 1:
        raise ValueError("checkpoint.restart_at_bar must leave a candle to continue")
    return index


def _engine_factory(
    config: BacktestAppConfig,
    performance: PerformanceConfig | None = None,
    *,
    scalable: bool = False,
    funding_source: RegisteredFundingSource | None = None,
    valuation_policy: str = VALUATION_POLICY_LAST,
    mark_price_source: RegisteredMarkPriceSource | None = None,
) -> Callable[[], BacktestEngine]:
    selected = performance or PerformanceConfig()
    policy = validate_valuation_inputs(valuation_policy, mark_price_source)
    execution_factory: ExecutionFactory | None
    if policy == VALUATION_POLICY_MARK:
        if mark_price_source is None:
            raise RuntimeError("validated mark valuation source disappeared")
        execution_factory = valuation_execution_factory(mark_price_source, funding_source)
    else:
        execution_factory = (
            funding_execution_factory(funding_source) if funding_source is not None else None
        )

    def create() -> BacktestEngine:
        registry = ModuleRegistry()
        modules = registry.load_config(config.modules)
        engine_config = config.core_engine_config()
        if scalable:
            engine_config = replace(
                engine_config,
                data=replace(
                    engine_config.data,
                    max_base_history=selected.max_base_history,
                ),
            )
            return ScalableBacktestEngine(
                modules=modules,
                config=engine_config,
                timeframes=_timeframe_specs(
                    config.requested_timeframes,
                    selected.max_timeframe_history,
                ),
                instrument=config.instrument_spec(),
                module_registry=registry,
                performance=selected,
                execution_factory=execution_factory,
            )
        return BacktestEngine(
            modules=modules,
            config=engine_config,
            timeframes=_timeframe_specs(config.requested_timeframes),
            instrument=config.instrument_spec(),
            module_registry=registry,
            execution_factory=execution_factory,
        )

    return create


def _scalable_engine_factory(
    config: BacktestAppConfig,
    performance: PerformanceConfig,
    funding_source: RegisteredFundingSource | None = None,
    *,
    valuation_policy: str = VALUATION_POLICY_LAST,
    mark_price_source: RegisteredMarkPriceSource | None = None,
) -> Callable[[], ScalableBacktestEngine]:
    factory = _engine_factory(
        config,
        performance,
        scalable=True,
        funding_source=funding_source,
        valuation_policy=valuation_policy,
        mark_price_source=mark_price_source,
    )

    def create() -> ScalableBacktestEngine:
        engine = factory()
        if not isinstance(engine, ScalableBacktestEngine):
            raise RuntimeError("scalable engine factory returned an incompatible engine")
        return engine

    return create


def _registered_checkpoint_uses_streaming(config: BacktestAppConfig) -> bool:
    if not config.data.is_registered:
        return False
    frozen = current_frozen_registered_data_options()
    if frozen is not None:
        return frozen[0] == "streaming"
    raw_mode = os.environ.get(_REGISTERED_MODE_ENV, config.data.registered_mode)
    mode = raw_mode.strip().lower()
    if mode not in _REGISTERED_MODES:
        supported = ", ".join(sorted(_REGISTERED_MODES))
        raise ValueError(f"registered dataset mode must be one of: {supported}")
    environment_batch = os.environ.get(_REGISTERED_BATCH_SIZE_ENV)
    if mode == "legacy_materialized" and environment_batch is not None:
        raise ValueError("registered stream batch size requires registered dataset mode streaming")
    return mode == "streaming"


def _json_text(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResultPublicationError("result payload is not canonical finite JSON") from exc


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    raise ResultPublicationError(f"result JSON contains non-finite numeric constant: {value}")


def _verify_strict_json_files(directory: Path) -> None:
    for path in sorted(directory.rglob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ResultPublicationError(f"result JSON path is not a regular file: {path}")
        try:
            json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_nonfinite_json_constant,
            )
        except ResultPublicationError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ResultPublicationError(f"result contains invalid JSON: {path}") from exc


def _raise_on_module_finish_errors(result: BacktestResult) -> None:
    errors = getattr(result.journal, "errors", ())
    failed = sorted(
        {
            str(row.get("stage"))
            for row in errors
            if isinstance(row, dict)
            and isinstance(row.get("stage"), str)
            and str(row["stage"]).startswith("module_finish:")
        }
    )
    if failed:
        raise ResultPublicationError(
            "module finalization failed; authoritative publication is forbidden: "
            + ",".join(failed)
        )


def _sync_fd(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = {errno.EINVAL}
        if hasattr(errno, "ENOTSUP"):
            unsupported.add(errno.ENOTSUP)
        if exc.errno not in unsupported or not hasattr(os, "sync"):
            raise
        os.sync()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        _sync_fd(stream.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        _sync_fd(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(directory: Path) -> None:
    directories = [directory]
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ResultPublicationError(f"result tree must not contain symlinks: {path}")
        if path.is_file():
            _fsync_file(path)
        elif path.is_dir():
            directories.append(path)
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        _fsync_directory(path)


def _recovery_application_metadata(replay: RecoveryRun) -> dict[str, object]:
    checkpoint = replay.checkpoint
    recovery_mode = getattr(replay, "recovery_mode", None)
    if recovery_mode is None and checkpoint is not None:
        recovery_mode = getattr(checkpoint, "recovery_mode", None)
    if recovery_mode is None:
        recovery_mode = "disabled"

    restarted = replay.restart_verified
    checkpoint_bars = getattr(checkpoint, "bars_processed", 0) if checkpoint is not None else 0
    return {
        "recovery_mode": recovery_mode,
        "simulation_passes": getattr(replay, "simulation_passes", 2 if restarted else 1),
        "source_traversals": getattr(replay, "source_traversals", 2 if restarted else 1),
        "source_rows_replayed": getattr(
            replay,
            "source_rows_replayed",
            checkpoint_bars if restarted else 0,
        ),
        "snapshot_schema_version": (
            getattr(checkpoint, "snapshot_schema_version", None) if checkpoint is not None else None
        ),
        "snapshot_bytes": (
            getattr(checkpoint, "snapshot_bytes", None) if checkpoint is not None else None
        ),
        "snapshot_event_stream_bytes": (
            getattr(checkpoint, "event_stream_bytes", None) if checkpoint is not None else None
        ),
    }


def _write_recovery_evidence(output: Path, replay: RecoveryRun) -> None:
    (output / "restart-evidence.json").write_text(
        _json_text(replay.evidence()),
        encoding="utf-8",
    )
    if replay.checkpoint is not None:
        (output / "replay-checkpoint.json").write_text(
            _json_text(replay.checkpoint.to_dict()),
            encoding="utf-8",
        )


def _write_result_files(
    output: Path,
    *,
    result: BacktestResult,
    replay: RecoveryRun,
    validation: ValidationSummary,
    config: BacktestAppConfig,
    provenance: SourceProvenance,
    performance: PerformanceConfig,
    application_id: str,
    funding_source: RegisteredFundingSource | None,
    valuation_policy: str = VALUATION_POLICY_LAST,
    mark_price_source: RegisteredMarkPriceSource | None = None,
) -> dict[str, object]:
    result.export_directory(output)
    _write_recovery_evidence(output, replay)
    instrument = config.instrument_spec()
    (output / "metrics-extended.json").write_text(
        _json_text(
            calculate_extended_metrics(
                trades=result.journal.trades,
                fills=result.journal.fills,
                equity=result.journal.equity,
                initial_equity=config.initial_capital,
                contract_size=instrument.contract_size,
            )
        ),
        encoding="utf-8",
    )
    (output / "data-validation.json").write_text(
        _json_text(validation.to_dict()),
        encoding="utf-8",
    )
    funding_metadata = funding_application_metadata(funding_source)
    funding_runtime: dict[str, object] | None = None
    if funding_source is not None:
        funding_runtime = dict(
            collect_funding_runtime_evidence(
                output,
                funding_source,
                application_id=application_id,
            )
        )
        (output / "funding-runtime.json").write_text(
            _json_text(funding_runtime),
            encoding="utf-8",
        )
    valuation_metadata: dict[str, object] = {}
    valuation_runtime: dict[str, object] | None = None
    if valuation_policy == VALUATION_POLICY_MARK:
        valuation_metadata = valuation_application_metadata(valuation_policy, mark_price_source)
        valuation_runtime = dict(
            valuation_runtime_evidence(
                application_id,
                valuation_policy,
                mark_price_source,
            )
        )
        (output / "valuation-runtime.json").write_text(
            _json_text(valuation_runtime),
            encoding="utf-8",
        )
    journal = result.journal
    runtime_counters: dict[str, int] | None = None
    if isinstance(journal, ScalableJournal):
        runtime_counters = journal.counters(result.bars_processed).to_dict()
    recovery_metadata = _recovery_application_metadata(replay)
    (output / "application-run.json").write_text(
        _json_text(
            {
                "application_version": APPLICATION_VERSION,
                "application_run_id": application_id,
                "core_run_id": result.run_manifest.run_id,
                "artifact_schema_version": (
                    journal.artifact_schema_version if isinstance(journal, ScalableJournal) else "1"
                ),
                "source_provenance": provenance.to_dict(),
                "seed": config.seed,
                **funding_metadata,
                "funding_runtime_evidence": funding_runtime,
                **valuation_metadata,
                **(
                    {"valuation_runtime_evidence": valuation_runtime}
                    if valuation_runtime is not None
                    else {}
                ),
                "market_type": config.data.market_type,
                "instrument_market_type": instrument.market_type,
                "contract_size": instrument.contract_size,
                "requested_timeframes": list(config.requested_timeframes),
                "checkpoint_mode": config.checkpoint.mode,
                **recovery_metadata,
                "restart_verified": replay.restart_verified,
                "continuous_sha256": replay.continuous_sha256,
                "restarted_sha256": replay.restarted_sha256,
                "performance": asdict(performance),
                "runtime_counters": runtime_counters,
                "validation": asdict(validation),
            }
        ),
        encoding="utf-8",
    )
    _verify_strict_json_files(output)
    core_run_id = result.run_manifest.run_id
    manifest = write_artifact_manifest(
        output,
        core_run_id,
        application_run_id=(application_id if application_id != core_run_id else None),
    )
    _fsync_tree(output)
    return manifest


def _destination_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _publish_result_directory(staging: Path, destination: Path) -> Path:
    staging_manifest = verify_artifact_manifest(staging)
    _fsync_tree(staging)
    if destination.is_symlink():
        raise ResultPublicationError(f"result destination must not be a symlink: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ResultPublicationError(f"result destination is not a directory: {destination}")
        try:
            existing_manifest = verify_artifact_manifest(destination)
        except ResultError as exc:
            raise ResultPublicationError(
                f"existing result directory is incomplete or corrupted: {destination}"
            ) from exc
        if existing_manifest != staging_manifest:
            raise ResultPublicationError(
                f"existing result directory conflicts with deterministic run: {destination}"
            )
        shutil.rmtree(staging)
        _fsync_directory(destination.parent)
        return destination

    try:
        staging.rename(destination)
        _fsync_directory(destination.parent)
    except OSError as exc:
        if _destination_present(destination):
            return _publish_result_directory(staging, destination)
        raise ResultPublicationError(f"cannot publish result directory: {destination}") from exc
    return destination


def _validated_run_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResultPublicationError("run manifest contains an empty run_id")
    if Path(value).name != value or "/" in value or "\\" in value:
        raise ResultPublicationError("run_id must be one safe path component")
    return value


def _write_failure_artifact(
    output_root: Path,
    error: ResourceLimitExceeded,
    performance: PerformanceConfig,
) -> Path:
    if output_root.is_symlink():
        raise ResultPublicationError(f"output directory must not be a symlink: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    name = f"failed-resource-limit-{timestamp}-{os.getpid()}"
    destination = output_root / name
    staging = Path(tempfile.mkdtemp(dir=output_root, prefix=f".{name}.staging-"))
    try:
        payload = {
            "schema_version": "1",
            "authoritative": False,
            "publication_rejected": True,
            "failure_type": type(error).__name__,
            "message": str(error),
            "performance": asdict(performance),
            "runtime_counters": (error.counters.to_dict() if error.counters is not None else None),
        }
        (staging / "failure.json").write_text(
            _json_text(payload) + "\n",
            encoding="utf-8",
        )
        _fsync_tree(staging)
        staging.rename(destination)
        _fsync_directory(output_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    setattr(error, "failure_directory", str(destination))
    return destination


def _assert_mark_source_matches_prepared(
    source: PreparedCandleSource,
    mark_price_source: RegisteredMarkPriceSource | None,
) -> None:
    if mark_price_source is None:
        return
    mark_price_source.assert_matches_candles(
        source.first_candle,
        source.last_candle,
        source.total_bars,
    )


def _run_single_pass(
    config: BacktestAppConfig,
    source: PreparedCandleSource,
    performance: PerformanceConfig,
    funding_source: RegisteredFundingSource | None = None,
    *,
    valuation_policy: str = VALUATION_POLICY_LAST,
    mark_price_source: RegisteredMarkPriceSource | None = None,
) -> ReplayRun:
    previous_random_state = random.getstate()
    try:
        random.seed(config.seed)
        engine = _scalable_engine_factory(
            config,
            performance,
            funding_source,
            valuation_policy=valuation_policy,
            mark_price_source=mark_price_source,
        )()
        result = engine.run_prepared(cast(PreparedCandleStream, source))
        return ReplayRun(result=result, checkpoint=None, restart_verified=False)
    finally:
        random.setstate(previous_random_state)


def run_backtest(
    config: BacktestAppConfig,
    performance: PerformanceConfig | None = None,
    *,
    funding_source: RegisteredFundingSource | None = None,
    valuation_policy: str = VALUATION_POLICY_LAST,
    mark_price_source: RegisteredMarkPriceSource | None = None,
) -> RunOutcome:
    selected = performance or PerformanceConfig()
    policy = validate_valuation_inputs(valuation_policy, mark_price_source)
    provenance = build_source_provenance()
    replay: RecoveryRun
    try:
        if config.checkpoint.mode == "disabled":
            source = prepare_candle_source(config.data)
            validation = source.validation
            _assert_mark_source_matches_prepared(source, mark_price_source)
            if policy == VALUATION_POLICY_LAST:
                replay = _run_single_pass(config, source, selected, funding_source)
            else:
                replay = _run_single_pass(
                    config,
                    source,
                    selected,
                    funding_source,
                    valuation_policy=policy,
                    mark_price_source=mark_price_source,
                )
        elif _registered_checkpoint_uses_streaming(config):
            if selected.mode != "canonical_audit":
                raise ValueError(
                    "checkpoint replay audit requires performance mode canonical_audit"
                )
            source = prepare_candle_source(config.data)
            if source.source_mode != "streaming":
                raise ValueError("registered checkpoint routing resolved a non-streaming source")
            validation = source.validation
            _assert_mark_source_matches_prepared(source, mark_price_source)
            restart_index = _restart_index(config, source.total_bars)
            if restart_index is None:
                raise RuntimeError("checkpoint replay did not resolve a restart boundary")
            replay = run_with_registered_streaming_replay_restart(
                _scalable_engine_factory(
                    config,
                    selected,
                    funding_source,
                    valuation_policy=policy,
                    mark_price_source=mark_price_source,
                ),
                source,
                restart_at_index=restart_index,
                seed=config.seed,
                commit_sha=provenance.source_commit_sha,
            )
        elif config.data.is_registered:
            if selected.mode != "canonical_audit":
                raise ValueError(
                    "checkpoint replay audit requires performance mode canonical_audit"
                )
            candles, validation = load_validated_candles(config.data)
            if mark_price_source is not None:
                mark_price_source.assert_matches_candles(candles[0], candles[-1], len(candles))
            restart_at_index = _restart_index(config, len(candles))
            if restart_at_index is None:
                raise RuntimeError("checkpoint replay did not resolve a restart boundary")
            replay = run_with_replay_restart(
                _engine_factory(
                    config,
                    selected,
                    scalable=False,
                    funding_source=funding_source,
                    valuation_policy=policy,
                    mark_price_source=mark_price_source,
                ),
                candles,
                restart_at_index=restart_at_index,
                seed=config.seed,
                commit_sha=provenance.source_commit_sha,
            )
        else:
            if selected.mode != "canonical_audit":
                raise ValueError(
                    "checkpoint snapshot audit requires performance mode canonical_audit"
                )
            source = prepare_candle_source(config.data)
            validation = source.validation
            _assert_mark_source_matches_prepared(source, mark_price_source)
            restart_at_index = _restart_index(config, source.total_bars)
            if restart_at_index is None:
                raise RuntimeError("checkpoint mode did not select a restart boundary")
            factory = _engine_factory(
                config,
                selected,
                scalable=True,
                funding_source=funding_source,
                valuation_policy=policy,
                mark_price_source=mark_price_source,
            )
            replay = run_with_runtime_snapshot_restart(
                cast(Any, factory),
                cast(Any, source),
                restart_at_index=restart_at_index,
                seed=config.seed,
                commit_sha=provenance.source_commit_sha,
            )
    except ResourceLimitExceeded as exc:
        _write_failure_artifact(config.output.directory, exc, selected)
        raise

    result = replay.result
    try:
        _raise_on_module_finish_errors(result)
        run_id = _validated_run_id(result.run_manifest.run_id)
        funding_app_run_id = _validated_run_id(application_run_id(run_id, funding_source))
        app_run_id = _validated_run_id(
            valuation_application_run_id(funding_app_run_id, policy, mark_price_source)
        )
        output_root = config.output.directory
        if output_root.is_symlink():
            raise ResultPublicationError(f"output directory must not be a symlink: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)
        if not output_root.is_dir():
            raise ResultPublicationError(f"output path is not a directory: {output_root}")
        destination = output_root / app_run_id
        staging = Path(
            tempfile.mkdtemp(
                dir=output_root,
                prefix=f".{app_run_id}.staging-",
            )
        )
        try:
            _write_result_files(
                staging,
                result=result,
                replay=replay,
                validation=validation,
                config=config,
                provenance=provenance,
                performance=selected,
                application_id=app_run_id,
                funding_source=funding_source,
                valuation_policy=policy,
                mark_price_source=mark_price_source,
            )
            output = _publish_result_directory(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    finally:
        if isinstance(result.journal, ScalableJournal):
            result.journal.streaming_store.cleanup()

    return RunOutcome(
        output_directory=output,
        validation=validation,
        run_id=run_id,
        bars_processed=result.bars_processed,
        restart_verified=replay.restart_verified,
        application_run_id=app_run_id,
    )
