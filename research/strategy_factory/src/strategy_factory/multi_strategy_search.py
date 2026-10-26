"""Governed owner-backed MS5A SEARCH preparation and execution boundary."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters import ResearchRunnerExecutionAdapter
from .canonical import canonical_json_bytes, canonical_sha256
from .checkpoint import AtomicCheckpointStore
from .contract_codec import decode_strategy_factory_request
from .contracts import FactoryStage, FactoryStatus, StrategyFactoryRequest
from .coordinator import FactoryCoordinator, StageHandler
from .dataset import DatasetValidationPolicy
from .errors import CompatibilityError, ExternalComponentUnavailableError
from .grammar import StrategyGrammar
from .multi_strategy_owner import (
    MS5A_DATASET_ID,
    MS5A_DATASET_VERSION,
    MS5A_FUNDING_VERSION,
    MS5A_REQUIRED_TIMEFRAMES,
    MS5ARegisteredDatasetAnalyzer,
    OwnerState,
    build_ms5a_owner_state,
)
from .multi_strategy_research_runtime import (
    RESULTS_PRODUCER_VERSION,
    RuntimePaths,
    build_ms5a_research_runtime,
)
from .multi_strategy_search_surface import (
    MS5A_RUNTIME_DATA,
    MS5ASearchSurface,
    build_ms5a_search_surface,
)
from .owner_runtime import PublishingResultsAggregator
from .planner import ArchitecturePlanner
from .promotion import ResultsAggregatorCandidateSelector
from .ranking import Constraint, Objective, ObjectiveDirection, ParetoRanker
from .run import StrategyFactoryRun
from .stage_handlers import (
    ArchitectureGenerationHandler,
    ExperimentPlanningHandler,
    ExperimentPlanningPolicy,
    HypothesisGenerationHandler,
    OptimizationPromotionHandler,
    RegimeDiscoveryHandler,
    StructuralScreeningHandler,
)

_FROZEN_REQUEST_FILE = "frozen-request.json"
_FROZEN_REQUEST_META_FILE = "frozen-request-meta.json"
_AUTHORITY_RECEIPT_FILE = "search-authority.json"
_SEARCH_ACTION = "MS5A_SEARCH"
_TARGET_STAGE = FactoryStage.WALK_FORWARD
_FORBIDDEN_ARTIFACT_STAGES = frozenset(
    {
        FactoryStage.WALK_FORWARD,
        FactoryStage.ROBUSTNESS,
        FactoryStage.HOLDOUT,
        FactoryStage.SIMPLICITY_SELECTION,
        FactoryStage.FINAL_AUDIT,
        FactoryStage.PACKAGE,
        FactoryStage.PAPER_ADMISSION,
    }
)


@dataclass(frozen=True, slots=True)
class MS5ASearchAuthority:
    """One exact, replay-safe SEARCH authority bound to source and request."""

    authority_id: str
    source_commit_sha: str
    request_fingerprint: str
    action: str = _SEARCH_ACTION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority_id, str)
            or not self.authority_id.strip()
            or self.authority_id != self.authority_id.strip()
        ):
            raise CompatibilityError("MS5A authority_id must be a canonical non-blank string")
        _sha(self.source_commit_sha, "MS5A authority source_commit_sha")
        _sha(self.request_fingerprint, "MS5A authority request_fingerprint", lengths=(64,))
        if self.action != _SEARCH_ACTION:
            raise CompatibilityError("MS5A authority action must be MS5A_SEARCH")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "authority_id": self.authority_id,
                "source_commit_sha": self.source_commit_sha,
                "request_fingerprint": self.request_fingerprint,
                "action": self.action,
            }
        )


@dataclass(frozen=True, slots=True)
class MS5AFrozenRequest:
    request: StrategyFactoryRequest
    source_commit_sha: str
    runtime_surface_fingerprint: str

    @property
    def summary(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "source_commit_sha": self.source_commit_sha,
            "request_id": self.request.request_id,
            "request_fingerprint": self.request.fingerprint,
            "dataset_id": self.request.dataset.dataset_id,
            "dataset_fingerprint": self.request.dataset.fingerprint,
            "dataset_sha256": self.request.dataset.sha256,
            "funding_sha256": self.request.dataset.funding_sha256,
            "funding_dataset_version": MS5A_FUNDING_VERSION,
            "dataset_row_count": self.request.dataset.row_count,
            "dataset_period": {
                "start": self.request.dataset.period.start.isoformat(),
                "end": self.request.dataset.period.end.isoformat(),
            },
            "request_created_at": self.request.created_at.isoformat(),
            "allowed_strategy_families": tuple(sorted(self.request.allowed_strategy_families)),
            "budget": {
                "maximum_experiments": self.request.budget.maximum_experiments,
                "maximum_architectures": self.request.budget.maximum_architectures,
                "maximum_finalists": self.request.budget.maximum_finalists,
                "maximum_duration_seconds": self.request.budget.maximum_duration_seconds,
                "maximum_memory_bytes": self.request.budget.maximum_memory_bytes,
                "maximum_cpu_workers": self.request.budget.maximum_cpu_workers,
                "maximum_parallel_tasks": self.request.budget.maximum_parallel_tasks,
                "stage_experiment_limits": dict(self.request.budget.stage_experiment_limits),
            },
            "runtime_surface_fingerprint": self.runtime_surface_fingerprint,
            "holdout_authorized": False,
            "walk_forward_authorized": False,
        }


class MS5ASearchComposition:
    """Own the pre-WALK_FORWARD Factory handlers and their external adapters."""

    def __init__(
        self,
        *,
        run: StrategyFactoryRun,
        owner: OwnerState,
        surface: MS5ASearchSurface,
        paths: RuntimePaths,
        market_data_root: Path,
        git_provenance: Any,
        environment_manifest: Any | None,
    ) -> None:
        if run.request != owner.request:
            raise CompatibilityError("MS5A run request differs from deep-verified owner request")
        if run.request.allowed_strategy_families != surface.allowed_strategy_families:
            raise CompatibilityError("MS5A request families differ from trusted search surface")

        runner, repository, research, provenance, environment = build_ms5a_research_runtime(
            run=run,
            owner=owner,
            surface=surface,
            paths=paths,
            market_data_root=market_data_root,
            git_provenance=git_provenance,
            environment_manifest=environment_manifest,
        )
        execution = ResearchRunnerExecutionAdapter(
            runner=runner,
            repository=repository,
            git_provenance=provenance,
            environment_manifest=environment,
        )
        aggregator = PublishingResultsAggregator(
            execution=execution,
            request=run.request,
            package_root=paths.results_package_root,
            registry_path=paths.results_registry_path,
            producer_module_version=RESULTS_PRODUCER_VERSION,
        )
        self._aggregator = aggregator

        planner = ArchitecturePlanner(
            StrategyGrammar(
                surface.catalog,
                required_runtime_contracts=surface.runtime_closure.feature_contracts,
            )
        )
        selector = ResultsAggregatorCandidateSelector(
            aggregator=aggregator,
            ranker=ParetoRanker(
                objectives=(
                    Objective("profit_factor", ObjectiveDirection.MAXIMIZE),
                    Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
                ),
                constraints=(
                    Constraint(
                        "trade_count",
                        minimum=Decimal(run.request.minimum_trade_count),
                    ),
                    Constraint(
                        "max_drawdown",
                        maximum=run.request.maximum_drawdown_fraction,
                    ),
                ),
            ),
        )
        analyzer = MS5ARegisteredDatasetAnalyzer(
            DatasetValidationPolicy(interval_seconds=300),
            storage_root=market_data_root,
            dataset_id=MS5A_DATASET_ID,
            dataset_version=MS5A_DATASET_VERSION,
            base_timeframe="5m",
            derived_timeframes=MS5A_REQUIRED_TIMEFRAMES,
            funding_dataset_id=MS5A_DATASET_ID,
            funding_dataset_version=MS5A_FUNDING_VERSION,
        )
        handlers: dict[FactoryStage, StageHandler] = {
            FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
            FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(surface.catalog),
            FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(
                planner,
                runtime_data=MS5A_RUNTIME_DATA,
                family_balanced=True,
            ),
            FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
                planner=planner,
                research_adapter=research,
                policy=ExperimentPlanningPolicy(search_method="staged"),
            ),
            FactoryStage.STRUCTURAL_SCREENING: StructuralScreeningHandler(
                research_adapter=research,
                execution_adapter=execution,
            ),
            FactoryStage.COARSE_SEARCH: OptimizationPromotionHandler(
                factory_stage=FactoryStage.COARSE_SEARCH,
                source_stage=FactoryStage.STRUCTURAL_SCREENING,
                optimization_stage_index=1,
                research_adapter=research,
                execution_adapter=execution,
                selector=selector,
                family_balanced=True,
            ),
            FactoryStage.LOCAL_REFINEMENT: OptimizationPromotionHandler(
                factory_stage=FactoryStage.LOCAL_REFINEMENT,
                source_stage=FactoryStage.COARSE_SEARCH,
                optimization_stage_index=2,
                research_adapter=research,
                execution_adapter=execution,
                selector=selector,
                family_balanced=True,
                freeze_finalists=True,
            ),
        }
        self._coordinator = FactoryCoordinator(
            run=run,
            dataset_analyzer=analyzer,
            handlers=handlers,
        )

    @property
    def coordinator(self) -> FactoryCoordinator:
        return self._coordinator

    def close(self) -> None:
        self._aggregator.close()


def _sha(value: object, name: str, *, lengths: tuple[int, ...] = (40, 64)) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in lengths
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompatibilityError(f"{name} must be lowercase hexadecimal")
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CompatibilityError(f"{name} must be a sequence")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"{name} must be canonical non-blank text")
    return value


def _safe_private_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise CompatibilityError("MS5A private runtime root must not be a symlink")
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise CompatibilityError("MS5A private runtime root must be a directory")
    return resolved


def _safe_private_child(root: Path, name: str) -> Path:
    candidate = root / name
    if candidate.exists() and candidate.is_symlink():
        raise CompatibilityError("MS5A private runtime child must not be a symlink")
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise CompatibilityError("MS5A private runtime child escaped its root")
    return resolved


def _write_exclusive(path: Path, payload: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise CompatibilityError(f"cannot create private MS5A artifact: {path.name}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CompatibilityError(f"cannot persist private MS5A artifact: {path.name}") from exc


def _surface_fingerprint(surface: MS5ASearchSurface) -> str:
    return canonical_sha256(
        {
            "source_commit_sha": surface.source_commit_sha,
            "families": tuple(sorted(surface.allowed_strategy_families)),
            "runtime_closure": surface.runtime_closure.fingerprint,
            "trusted_runtime_modules": tuple(
                (
                    item.runtime_component_id,
                    item.runtime_component_version,
                    item.import_path,
                    item.source_commit_sha,
                )
                for item in surface.trusted_runtime_modules
            ),
        }
    )


def freeze_ms5a_request(
    *,
    market_data_root: str | Path,
    runtime_root: str | Path,
    source_commit_sha: str,
    created_at: datetime,
) -> MS5AFrozenRequest:
    """Deep-verify and persist the exact request without starting Research execution."""

    _sha(source_commit_sha, "MS5A source_commit_sha", lengths=(40,))
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise CompatibilityError("MS5A request created_at must be timezone-aware")
    root = _safe_private_root(runtime_root)
    request_path = root / _FROZEN_REQUEST_FILE
    meta_path = root / _FROZEN_REQUEST_META_FILE
    surface = build_ms5a_search_surface(source_commit_sha=source_commit_sha)
    surface_fingerprint = _surface_fingerprint(surface)

    if request_path.exists() or meta_path.exists():
        if request_path.is_symlink() or meta_path.is_symlink():
            raise CompatibilityError("frozen MS5A request artifacts must not be symlinks")
        if not request_path.is_file() or not meta_path.is_file():
            raise CompatibilityError("frozen MS5A request artifacts are incomplete")
        try:
            request = decode_strategy_factory_request(
                json.loads(request_path.read_text(encoding="utf-8"))
            )
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompatibilityError("frozen MS5A request artifacts are unreadable") from exc
        if not isinstance(metadata, Mapping):
            raise CompatibilityError("frozen MS5A request metadata is malformed")
        if metadata.get("source_commit_sha") != source_commit_sha:
            raise CompatibilityError("frozen MS5A request belongs to another exact source")
        if metadata.get("request_fingerprint") != request.fingerprint:
            raise CompatibilityError("frozen MS5A request fingerprint metadata drifted")
        if metadata.get("runtime_surface_fingerprint") != surface_fingerprint:
            raise CompatibilityError("frozen MS5A runtime surface changed")

        current = build_ms5a_owner_state(
            market_data_root,
            created_at=request.created_at,
        )
        if current.request != request:
            raise CompatibilityError("deep-verified MS5A owner request changed after freeze")
        return MS5AFrozenRequest(request, source_commit_sha, surface_fingerprint)

    owner = build_ms5a_owner_state(
        market_data_root,
        created_at=created_at.astimezone(UTC),
    )
    if owner.request.allowed_strategy_families != surface.allowed_strategy_families:
        raise CompatibilityError("MS5A owner request and trusted surface family sets differ")
    request_raw = canonical_json_bytes(owner.request) + b"\n"
    metadata = {
        "schema_version": 1,
        "source_commit_sha": source_commit_sha,
        "request_fingerprint": owner.request.fingerprint,
        "runtime_surface_fingerprint": surface_fingerprint,
    }
    try:
        _write_exclusive(request_path, request_raw)
        _write_exclusive(
            meta_path,
            json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        )
    except FileExistsError as exc:
        raise CompatibilityError("concurrent MS5A request freeze detected") from exc
    return MS5AFrozenRequest(owner.request, source_commit_sha, surface_fingerprint)


def _load_frozen_request(
    *,
    market_data_root: str | Path,
    runtime_root: str | Path,
    source_commit_sha: str,
) -> tuple[MS5AFrozenRequest, OwnerState, MS5ASearchSurface]:
    root = _safe_private_root(runtime_root)
    request_path = root / _FROZEN_REQUEST_FILE
    meta_path = root / _FROZEN_REQUEST_META_FILE
    if request_path.is_symlink() or meta_path.is_symlink():
        raise CompatibilityError("MS5A frozen request artifacts must not be symlinks")
    if not request_path.is_file() or not meta_path.is_file():
        raise CompatibilityError("MS5A SEARCH requires a previously frozen request")
    try:
        request = decode_strategy_factory_request(
            json.loads(request_path.read_text(encoding="utf-8"))
        )
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompatibilityError("MS5A frozen request artifacts are unreadable") from exc
    if not isinstance(metadata, Mapping):
        raise CompatibilityError("MS5A frozen request metadata is malformed")
    surface = build_ms5a_search_surface(source_commit_sha=source_commit_sha)
    surface_fingerprint = _surface_fingerprint(surface)
    if (
        metadata.get("source_commit_sha") != source_commit_sha
        or metadata.get("request_fingerprint") != request.fingerprint
        or metadata.get("runtime_surface_fingerprint") != surface_fingerprint
    ):
        raise CompatibilityError("MS5A frozen request binding is invalid")
    owner = build_ms5a_owner_state(
        market_data_root,
        created_at=request.created_at,
    )
    if owner.request != request:
        raise CompatibilityError("MS5A live owner state differs from frozen request")
    return MS5AFrozenRequest(request, source_commit_sha, surface_fingerprint), owner, surface


def _consume_authority(
    root: Path,
    authority: MS5ASearchAuthority,
    *,
    run_id: str,
) -> None:
    path = root / _AUTHORITY_RECEIPT_FILE
    payload = {
        "schema_version": 1,
        "authority_id": authority.authority_id,
        "authority_fingerprint": authority.fingerprint,
        "source_commit_sha": authority.source_commit_sha,
        "request_fingerprint": authority.request_fingerprint,
        "run_id": run_id,
        "action": authority.action,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise CompatibilityError("MS5A authority receipt is unsafe")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompatibilityError("MS5A authority receipt is unreadable") from exc
        if existing != payload:
            raise CompatibilityError(
                "MS5A SEARCH authority was already consumed by another lineage"
            )
        return
    try:
        _write_exclusive(path, raw)
    except FileExistsError:
        _consume_authority(root, authority, run_id=run_id)


def _runtime_paths(root: Path) -> RuntimePaths:
    execution_root = _safe_private_child(root, "execution")
    research_root = _safe_private_child(execution_root, "research")
    results_root = _safe_private_child(execution_root, "results")
    return RuntimePaths(
        factory_state_root=_safe_private_child(execution_root, "factory"),
        research_state_path=research_root / "research.sqlite3",
        research_artifact_root=_safe_private_child(research_root, "artifacts"),
        research_output_root=_safe_private_child(research_root, "output"),
        results_package_root=_safe_private_child(results_root, "packages"),
        results_registry_path=results_root / "registry.sqlite3",
    )


def _git_provenance(expected_head: str) -> tuple[Any, Any | None]:
    _sha(expected_head, "MS5A expected_head", lengths=(40,))
    try:
        root_text = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        root = Path(root_text).resolve(strict=True)
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ("git", "branch", "--show-current"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CompatibilityError("MS5A trusted Git checkout cannot be verified") from exc
    if head != expected_head:
        raise CompatibilityError("MS5A checkout HEAD differs from exact authority")
    if status:
        raise CompatibilityError("MS5A checkout must be clean including untracked files")
    try:
        from modular_research_runner import GitProvenance
    except ImportError as exc:
        raise ExternalComponentUnavailableError("Research Runner is unavailable") from exc
    return (
        GitProvenance(
            branch=branch or f"detached-{head[:12]}",
            commit_sha=head,
            clean=True,
            diff_sha256=None,
            untracked_policy="reject",
        ),
        None,
    )


def _module_versions(surface: MS5ASearchSurface) -> dict[str, str]:
    versions: dict[str, str] = {}
    for item in surface.trusted_runtime_modules:
        prior = versions.get(item.runtime_component_id)
        if prior is not None and prior != item.runtime_component_version:
            raise CompatibilityError("MS5A trusted runtime component version conflict")
        versions[item.runtime_component_id] = item.runtime_component_version
    return dict(sorted(versions.items()))


def _advance_to_walk_forward(
    run: StrategyFactoryRun,
    coordinator: FactoryCoordinator,
    *,
    maximum_steps: int = 20,
) -> None:
    for _ in range(maximum_steps):
        checkpoint = run.initialize()
        if checkpoint.stage is _TARGET_STAGE:
            return
        if checkpoint.stage in _FORBIDDEN_ARTIFACT_STAGES:
            raise CompatibilityError("MS5A SEARCH crossed its hard pre-WALK_FORWARD boundary")
        if checkpoint.status in {
            FactoryStatus.CANCELED,
            FactoryStatus.COMPLETED,
            FactoryStatus.FAILED,
            FactoryStatus.DATASET_REJECTED,
            FactoryStatus.BUDGET_EXHAUSTED,
            FactoryStatus.CORRUPTED,
            FactoryStatus.BLOCKED,
            FactoryStatus.PAUSED,
        }:
            raise CompatibilityError(
                f"MS5A SEARCH stopped before WALK_FORWARD: {checkpoint.status.value}"
            )
        result = coordinator.step()
        if result.action not in {"completed", "skipped"}:
            raise CompatibilityError(
                f"MS5A SEARCH stage did not complete: {result.stage.value}/{result.action}"
            )
    raise CompatibilityError("MS5A SEARCH step limit reached before WALK_FORWARD")


def _artifact(run: StrategyFactoryRun, stage: FactoryStage) -> Mapping[str, Any] | None:
    artifact_id = run.checkpoint.completed_artifacts.get(stage.value)
    if artifact_id is None:
        return None
    value = run.load_artifact(artifact_id)
    if not isinstance(value, Mapping):
        raise CompatibilityError(f"MS5A {stage.value} artifact is malformed")
    return value


def _search_summary(run: StrategyFactoryRun, authority: MS5ASearchAuthority) -> dict[str, Any]:
    checkpoint = run.initialize()
    if checkpoint.stage is not _TARGET_STAGE:
        raise CompatibilityError("MS5A summary requires the WALK_FORWARD boundary")
    forbidden = sorted(
        stage.value
        for stage in _FORBIDDEN_ARTIFACT_STAGES
        if stage.value in checkpoint.completed_artifacts
    )
    if forbidden:
        raise CompatibilityError("MS5A SEARCH produced forbidden downstream artifacts")

    stages: dict[str, Any] = {}
    for stage in (
        FactoryStage.ARCHITECTURE_GENERATION,
        FactoryStage.EXPERIMENT_PLANNING,
        FactoryStage.STRUCTURAL_SCREENING,
        FactoryStage.COARSE_SEARCH,
        FactoryStage.LOCAL_REFINEMENT,
    ):
        artifact = _artifact(run, stage)
        if artifact is None:
            raise CompatibilityError(f"MS5A SEARCH lacks {stage.value} evidence")
        stages[stage.value] = {
            "family_allocations": artifact.get("family_allocations", ()),
            "unused_stage_capacity": artifact.get(
                "unused_stage_capacity",
                artifact.get("unused_architecture_capacity", 0),
            ),
            "experiments": tuple(
                {
                    "experiment_id": item.get("experiment_id"),
                    "factory_plan_id": item.get("factory_plan_id"),
                    "status": item.get("status"),
                    "run_ids": item.get("run_ids", ()),
                    "strategy_family": item.get("strategy_family"),
                }
                for item in artifact.get("experiments", ())
                if isinstance(item, Mapping)
            ),
        }

    local_refinement = _artifact(run, FactoryStage.LOCAL_REFINEMENT)
    if local_refinement is None:
        raise CompatibilityError("MS5A SEARCH lacks local refinement finalist evidence")
    if local_refinement.get("finalist_selection_policy") != "global-pareto-finalists-v1":
        raise CompatibilityError("MS5A SEARCH finalist selection policy drifted")
    selection_fingerprint = _sha(
        local_refinement.get("finalist_selection_fingerprint"),
        "MS5A finalist selection fingerprint",
        lengths=(64,),
    )
    finalists: list[dict[str, Any]] = []
    for raw in _sequence(local_refinement.get("finalists"), "search finalists"):
        record = _mapping(raw, "search finalist")
        candidate = _mapping(record.get("candidate"), "search finalist candidate")
        finalists.append(
            {
                "candidate_id": _text(candidate.get("candidate_id"), "candidate_id"),
                "configuration_hash": _sha(
                    candidate.get("configuration_hash"),
                    "candidate configuration_hash",
                    lengths=(64,),
                ),
                "source_experiment_id": _text(
                    record.get("source_experiment_id"),
                    "source_experiment_id",
                ),
                "source_factory_plan_id": _text(
                    record.get("source_factory_plan_id"),
                    "source_factory_plan_id",
                ),
                "strategy_family": _text(
                    record.get("strategy_family"),
                    "strategy_family",
                ),
                "selection_reasons": tuple(record.get("selection_reasons", ())),
            }
        )
    expected_count = local_refinement.get("finalist_count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
        or expected_count != len(finalists)
    ):
        raise CompatibilityError("MS5A SEARCH finalist count evidence is invalid")
    phase_outcome = "finalists_frozen" if finalists else "no_admissible_candidate"

    return {
        "schema_version": 1,
        "phase": "search",
        "phase_outcome": phase_outcome,
        "target_stage": FactoryStage.WALK_FORWARD.value,
        "run_id": run.run_id,
        "request_id": run.request.request_id,
        "request_fingerprint": run.request.fingerprint,
        "commit_sha": run.commit_sha,
        "authority_id": authority.authority_id,
        "authority_fingerprint": authority.fingerprint,
        "status": checkpoint.status.value,
        "next_stage": checkpoint.stage.value,
        "stage_evidence": stages,
        "finalist_selection_policy": "global-pareto-finalists-v1",
        "finalist_selection_fingerprint": selection_fingerprint,
        "finalists": tuple(finalists),
        "holdout_opened": False,
        "final_test_grants": 0,
        "walk_forward_executed": False,
        "live_trading_authorized": False,
    }


def execute_authorized_ms5a_search(
    *,
    authority: MS5ASearchAuthority,
    expected_head: str,
    market_data_root: str | Path,
    runtime_root: str | Path,
) -> dict[str, Any]:
    """Execute or resume exactly one authorized SEARCH and stop before WALK_FORWARD."""

    if authority.source_commit_sha != expected_head:
        raise CompatibilityError("MS5A authority source SHA differs from requested checkout")
    frozen, owner, surface = _load_frozen_request(
        market_data_root=market_data_root,
        runtime_root=runtime_root,
        source_commit_sha=expected_head,
    )
    if authority.request_fingerprint != frozen.request.fingerprint:
        raise CompatibilityError("MS5A authority request fingerprint differs from frozen request")

    root = _safe_private_root(runtime_root)
    paths = _runtime_paths(root)
    provisional = StrategyFactoryRun(
        request=frozen.request,
        commit_sha=expected_head,
        state_root=paths.factory_state_root,
        module_versions=_module_versions(surface),
        now=frozen.request.created_at,
    )
    _consume_authority(root, authority, run_id=provisional.run_id)
    git_provenance, environment_manifest = _git_provenance(expected_head)

    run_root = paths.factory_state_root / provisional.run_id
    checkpoint_store = AtomicCheckpointStore(run_root / "checkpoints")
    if checkpoint_store.exists(provisional.run_id):
        run = StrategyFactoryRun.restore_existing(
            state_root=paths.factory_state_root,
            run_id=provisional.run_id,
        )
        if run.request != frozen.request or run.commit_sha != expected_head:
            raise CompatibilityError("restored MS5A SEARCH lineage differs from authority")
    else:
        run = provisional

    composition = MS5ASearchComposition(
        run=run,
        owner=owner,
        surface=surface,
        paths=paths,
        market_data_root=Path(market_data_root).expanduser().resolve(strict=True),
        git_provenance=git_provenance,
        environment_manifest=environment_manifest,
    )
    try:
        _advance_to_walk_forward(run, composition.coordinator)
        return _search_summary(run, authority)
    finally:
        composition.close()


__all__ = [
    "MS5AFrozenRequest",
    "MS5ASearchAuthority",
    "MS5ASearchComposition",
    "execute_authorized_ms5a_search",
    "freeze_ms5a_request",
]
