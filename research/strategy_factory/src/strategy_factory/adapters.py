"""Stable ports to Research Runner, Results Aggregator, and Strategy Audit."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .canonical import canonical_json_bytes, canonical_sha256
from .contracts import (
    CandidateEvaluation,
    CandidateStrategy,
    ExperimentPlan,
    FactoryStage,
    ParameterSpace,
    StrategyArchitecture,
    StrategyFactoryRequest,
    StrategyPackage,
    TimeRange,
)
from .errors import CompatibilityError, ExternalComponentUnavailableError


@dataclass(frozen=True, slots=True)
class ResearchSplitPlan:
    train: TimeRange
    validation: TimeRange
    test: TimeRange
    holdout: TimeRange
    gap_seconds: int = 0

    def __post_init__(self) -> None:
        if not (self.train.end <= self.validation.start <= self.validation.end):
            raise CompatibilityError("train and validation ranges overlap")
        if not (self.validation.end <= self.test.start <= self.test.end):
            raise CompatibilityError("validation and test ranges overlap")
        if not (self.test.end <= self.holdout.start <= self.holdout.end):
            raise CompatibilityError("test and holdout ranges overlap")
        if self.gap_seconds < 0:
            raise CompatibilityError("split gap_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class PreparedResearchPlan:
    factory_plan: ExperimentPlan
    native_specification: Any
    native_plan: Any
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResearchSubmission:
    factory_plan_id: str
    experiment_id: str
    specification_fingerprint: str
    plan_fingerprint: str
    manifest_fingerprint: str


@dataclass(frozen=True, slots=True)
class ResearchFinalTestAuthorization:
    grant_id: str
    experiment_id: str
    specification_fingerprint: str
    split_fingerprint: str
    candidate_configuration_hashes: tuple[str, ...]
    issued_at: datetime
    consumed_at: datetime | None

    def __post_init__(self) -> None:
        for name in ("grant_id", "experiment_id"):
            _nonblank(getattr(self, name), name)
        for name in ("specification_fingerprint", "split_fingerprint"):
            _digest(getattr(self, name), name)
        candidates = tuple(sorted(set(self.candidate_configuration_hashes)))
        if not candidates:
            raise CompatibilityError("final-test authorization requires candidates")
        for value in candidates:
            _digest(value, "candidate configuration hash")
        object.__setattr__(self, "candidate_configuration_hashes", candidates)
        object.__setattr__(self, "issued_at", _utc_datetime(self.issued_at, "issued_at"))
        if self.consumed_at is not None:
            consumed = _utc_datetime(self.consumed_at, "consumed_at")
            if consumed < self.issued_at:
                raise CompatibilityError("final-test consumption predates authorization")
            object.__setattr__(self, "consumed_at", consumed)


@dataclass(frozen=True, slots=True)
class ResearchFinalTestActivation:
    authorization: ResearchFinalTestAuthorization
    run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.authorization.consumed_at is None:
            raise CompatibilityError("final-test activation must return a consumed grant")
        if not self.run_ids:
            raise CompatibilityError("final-test activation requires run IDs")
        for run_id in self.run_ids:
            _nonblank(run_id, "final-test run_id")


@dataclass(frozen=True, slots=True)
class ResearchStageActivation:
    experiment_id: str
    target_role: str
    candidate_configuration_hashes: tuple[str, ...]
    run_ids: tuple[str, ...]
    activated_at: datetime
    optimization_stage_index: int | None = None

    def __post_init__(self) -> None:
        _nonblank(self.experiment_id, "experiment_id")
        _nonblank(self.target_role, "target_role")
        candidates = tuple(sorted(set(self.candidate_configuration_hashes)))
        if not candidates:
            raise CompatibilityError("stage activation requires frozen candidates")
        for candidate in candidates:
            _digest(candidate, "candidate configuration hash")
        if not self.run_ids:
            raise CompatibilityError("stage activation requires run IDs")
        for run_id in self.run_ids:
            _nonblank(run_id, "stage activation run_id")
        if self.optimization_stage_index is not None and (
            isinstance(self.optimization_stage_index, bool)
            or not isinstance(self.optimization_stage_index, int)
            or self.optimization_stage_index < 1
        ):
            raise CompatibilityError("optimization_stage_index must be a positive int or None")
        object.__setattr__(self, "candidate_configuration_hashes", candidates)
        object.__setattr__(
            self,
            "activated_at",
            _utc_datetime(self.activated_at, "activated_at"),
        )


@dataclass(frozen=True, slots=True)
class ResearchFrozenConfiguration:
    native_configuration_hash: str
    factory_configuration_hash: str
    parameters: Mapping[str, Any]
    architecture_id: str | None = None
    architecture_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _digest(self.native_configuration_hash, "native_configuration_hash")
        _digest(self.factory_configuration_hash, "factory_configuration_hash")
        if not isinstance(self.parameters, Mapping):
            raise CompatibilityError("frozen configuration parameters must be a mapping")
        if canonical_sha256(self.parameters) != self.factory_configuration_hash:
            raise CompatibilityError("Factory configuration hash does not match parameters")
        if (self.architecture_id is None) != (self.architecture_fingerprint is None):
            raise CompatibilityError("frozen architecture identity must be complete or absent")
        if self.architecture_id is not None:
            _nonblank(self.architecture_id, "architecture_id")
            _digest(self.architecture_fingerprint, "architecture_fingerprint")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ResearchExperimentSnapshot:
    experiment_id: str
    status: str
    run_status_counts: Mapping[str, int]
    run_ids: tuple[str, ...]
    has_nonterminal_runs: bool


@dataclass(frozen=True, slots=True)
class StructuredExternalFailure:
    operation: str
    error_type: str
    reason_code: str
    message: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class ResearchControlResult:
    snapshot: ResearchExperimentSnapshot | None
    failure: StructuredExternalFailure | None

    def __post_init__(self) -> None:
        if (self.snapshot is None) == (self.failure is None):
            raise CompatibilityError(
                "research control result requires exactly one of snapshot or failure"
            )


class ResearchRunnerExecutionAdapter:
    """Stable control API over an injected Research Runner and its repository."""

    def __init__(
        self,
        *,
        runner: Any,
        repository: Any,
        git_provenance: Any,
        environment_manifest: Any | None = None,
    ) -> None:
        for name, method in (
            ("runner", "prepare"),
            ("runner", "execute"),
            ("runner", "authorize_final_test"),
            ("runner", "activate_final_test"),
            ("runner", "activate_stage"),
            ("runner", "activate_optimization_stage"),
            ("repository", "experiment_status"),
            ("repository", "counts"),
            ("repository", "list_runs"),
            ("repository", "has_nonterminal_runs"),
            ("repository", "cancel_pending"),
            ("repository", "get_final_test_grant"),
        ):
            target = runner if name == "runner" else repository
            if not callable(getattr(target, method, None)):
                raise TypeError(f"{name} must implement {method}()")
        self._runner = runner
        self._repository = repository
        self._git = git_provenance
        self._environment = environment_manifest
        self._cancellations: dict[str, Any] = {}
        self._prepared: dict[str, Any] = {}
        self._factory_plan_experiments: dict[str, str] = {}

    def submit(self, prepared: PreparedResearchPlan) -> ResearchSubmission:
        known_experiment = self._factory_plan_experiments.get(prepared.factory_plan.plan_id)
        if known_experiment is not None:
            native = self._prepared.get(known_experiment)
            if native is None:
                raise CompatibilityError(
                    "cached Factory plan points to a missing prepared experiment"
                )
            expected_semantic = _research_plan_semantic_fingerprint(prepared.native_plan)
            if _research_plan_semantic_fingerprint(native.plan) != expected_semantic:
                raise CompatibilityError(
                    "Factory plan ID was reused with different Research Runner semantics"
                )
            return _submission_from_native(prepared.factory_plan.plan_id, native)

        prepare_kwargs: dict[str, Any] = {"git": self._git}
        if self._environment is not None:
            prepare_kwargs["environment"] = self._environment
        if prepared.created_at is not None:
            prepare_kwargs["created_at"] = _utc_datetime(
                prepared.created_at, "prepared research created_at"
            )
        native = self._runner.prepare(
            prepared.native_specification,
            **prepare_kwargs,
        )
        expected_semantic = _research_plan_semantic_fingerprint(prepared.native_plan)
        actual_semantic = _research_plan_semantic_fingerprint(native.plan)
        if actual_semantic != expected_semantic:
            raise CompatibilityError(
                "Research Runner prepared plan differs semantically from the frozen preview"
            )
        actual = _fingerprint(native.plan, "prepared research plan")
        experiment_id = _nonblank(
            getattr(native.manifest, "experiment_id", None),
            "prepared experiment_id",
        )
        submission = ResearchSubmission(
            factory_plan_id=prepared.factory_plan.plan_id,
            experiment_id=experiment_id,
            specification_fingerprint=_fingerprint(native.specification, "research specification"),
            plan_fingerprint=actual,
            manifest_fingerprint=_fingerprint(native.manifest, "research manifest"),
        )
        existing = self._prepared.get(experiment_id)
        if existing is not None:
            if (
                _fingerprint(existing.specification, "existing research specification")
                != submission.specification_fingerprint
                or _fingerprint(existing.plan, "existing research plan")
                != submission.plan_fingerprint
                or _fingerprint(existing.manifest, "existing research manifest")
                != submission.manifest_fingerprint
            ):
                raise CompatibilityError(
                    "Research Runner experiment identity was reused with different content"
                )
        self._prepared[experiment_id] = native
        self._factory_plan_experiments[prepared.factory_plan.plan_id] = experiment_id
        return submission

    def expected_experiment_id(self, prepared: PreparedResearchPlan) -> str:
        """Derive the native experiment identity without registering or mutating it."""

        try:
            import modular_research_runner as research
        except ImportError as exc:  # pragma: no cover - constructor already validated
            raise ExternalComponentUnavailableError(
                "Research Runner is unavailable during identity derivation"
            ) from exc
        return research.ExperimentIdentityInputs(
            specification=prepared.native_specification,
            backtest_engine_version=_nonblank(
                getattr(self._runner, "backtest_engine_version", None),
                "backtest_engine_version",
            ),
            metric_schema_version=_nonblank(
                getattr(self._runner, "metric_schema_version", None),
                "metric_schema_version",
            ),
            module_api_version=_nonblank(
                getattr(self._runner, "module_api_version", None),
                "module_api_version",
            ),
        ).experiment_id()

    def ensure_submission(self, prepared: PreparedResearchPlan) -> ResearchSubmission:
        """Create a new experiment or cold-restore an already registered one."""

        experiment_id = self.expected_experiment_id(prepared)
        try:
            self._repository.experiment_status(experiment_id)
        except Exception as exc:
            if (
                type(exc).__name__ != "StateRepositoryError"
                or "not registered" not in str(exc).lower()
            ):
                raise CompatibilityError(
                    "Research Runner repository identity check failed"
                ) from exc
            submission = self.submit(prepared)
            if submission.experiment_id != experiment_id:
                raise CompatibilityError(
                    "Research Runner registered an unexpected experiment identity"
                ) from exc
            return submission
        return self.restore_submission(
            prepared,
            expected_experiment_id=experiment_id,
        )

    def restore_submission(
        self,
        prepared: PreparedResearchPlan,
        *,
        expected_experiment_id: str | None = None,
    ) -> ResearchSubmission:
        """Restore a registered experiment without mutating its scheduler status.

        ``ResearchRunner.prepare`` is intentionally not called when an expected
        experiment identity is supplied and the in-memory cache is empty. The
        upstream method always writes ``PLANNED`` and is therefore unsafe during
        cold recovery after a completed stage. Instead, this adapter verifies the
        content-addressed native artifacts and reconstructs the immutable
        ``PreparedExperiment`` from those artifacts plus the deterministic plan
        builder.
        """

        if expected_experiment_id is None:
            return self.submit(prepared)
        experiment_id = _nonblank(expected_experiment_id, "expected_experiment_id")
        known_experiment = self._factory_plan_experiments.get(prepared.factory_plan.plan_id)
        if known_experiment is not None and known_experiment != experiment_id:
            raise CompatibilityError(
                "Factory plan is already bound to another Research Runner experiment"
            )
        native = self._prepared.get(experiment_id)
        if native is None:
            native = self._restore_native_prepared(prepared, experiment_id)
            self._prepared[experiment_id] = native
        expected_semantic = _research_plan_semantic_fingerprint(prepared.native_plan)
        if _research_plan_semantic_fingerprint(native.plan) != expected_semantic:
            raise CompatibilityError(
                "restored Research Runner plan differs from the frozen Factory plan"
            )
        self._factory_plan_experiments[prepared.factory_plan.plan_id] = experiment_id
        return _submission_from_native(prepared.factory_plan.plan_id, native)

    def _restore_native_prepared(
        self,
        prepared: PreparedResearchPlan,
        experiment_id: str,
    ) -> Any:
        try:
            import modular_research_runner as research
        except ImportError as exc:  # pragma: no cover - constructor already validated
            raise ExternalComponentUnavailableError(
                "Research Runner is unavailable during restore"
            ) from exc

        identity = self.expected_experiment_id(prepared)
        if identity != experiment_id:
            raise CompatibilityError(
                "stored Research Runner experiment ID does not match frozen specification"
            )
        plan_builder = getattr(self._runner, "plan_builder", None)
        build_plan = getattr(plan_builder, "build", None)
        if not callable(build_plan):
            raise CompatibilityError(
                "Research Runner does not expose a deterministic plan builder for recovery"
            )
        native_plan = build_plan(
            experiment_id=experiment_id,
            specification=prepared.native_specification,
        )
        if _research_plan_semantic_fingerprint(native_plan) != _research_plan_semantic_fingerprint(
            prepared.native_plan
        ):
            raise CompatibilityError(
                "cold-restored Research Runner plan differs semantically from preview"
            )

        artifact_store = getattr(self._runner, "artifact_store", None)
        if artifact_store is None or not callable(getattr(artifact_store, "read_json", None)):
            raise CompatibilityError(
                "Research Runner artifact store is unavailable during cold restore"
            )
        root = f"experiments/{experiment_id}"
        specification_reference = _native_artifact_reference(
            research,
            artifact_store,
            f"{root}/specification.json",
        )
        manifest_reference = _native_artifact_reference(
            research,
            artifact_store,
            f"{root}/manifest.json",
        )
        plan_reference = _native_artifact_reference(
            research,
            artifact_store,
            f"{root}/plan.json",
        )
        if specification_reference.sha256 != _fingerprint(
            prepared.native_specification,
            "research specification",
        ):
            raise CompatibilityError(
                "stored Research Runner specification artifact does not match frozen input"
            )
        if plan_reference.sha256 != _fingerprint(
            native_plan,
            "restored research plan",
        ):
            raise CompatibilityError(
                "stored Research Runner plan artifact does not match deterministic rebuild"
            )
        manifest_payload = artifact_store.read_json(manifest_reference)
        try:
            restored_payload = research.restore_canonical_value(manifest_payload)
            manifest = _decode_native_manifest(research, restored_payload)
        except Exception as exc:
            raise CompatibilityError(
                "stored Research Runner manifest cannot be decoded safely"
            ) from exc
        if manifest.experiment_id != experiment_id:
            raise CompatibilityError(
                "stored Research Runner manifest has a different experiment ID"
            )
        if manifest.specification_sha256 != _fingerprint(
            prepared.native_specification,
            "research specification",
        ):
            raise CompatibilityError(
                "stored Research Runner manifest references another specification"
            )
        if manifest.configuration_hashes != tuple(
            item.sha256 for item in native_plan.configurations
        ):
            raise CompatibilityError(
                "stored Research Runner manifest configuration population changed"
            )
        if manifest.fingerprint != manifest_reference.sha256:
            raise CompatibilityError(
                "stored Research Runner manifest fingerprint does not match artifact"
            )
        try:
            self._repository.experiment_status(experiment_id)
        except Exception as exc:
            raise CompatibilityError(
                "Research Runner repository has no registered experiment for artifacts"
            ) from exc
        return research.PreparedExperiment(
            specification=prepared.native_specification,
            manifest=manifest,
            plan=native_plan,
            specification_artifact=specification_reference,
            manifest_artifact=manifest_reference,
            plan_artifact=plan_reference,
        )

    def frozen_configurations(self, experiment_id: str) -> tuple[ResearchFrozenConfiguration, ...]:
        """Return exact hash/parameter pairs frozen in the native plan."""

        native = self._prepared_experiment(experiment_id)
        configurations = getattr(native.plan, "configurations", None)
        if not isinstance(configurations, tuple):
            raise CompatibilityError("Research Runner plan configurations must be a tuple")
        frozen_items: list[ResearchFrozenConfiguration] = []
        for item in configurations:
            native_parameters = getattr(item, "parameters", None)
            if not isinstance(native_parameters, Mapping):
                raise CompatibilityError(
                    "Research Runner configuration parameters must be a mapping"
                )
            factory_parameters = _strategy_parameters_from_native(native_parameters)
            architecture_id, architecture_fingerprint = _native_architecture_identity(
                native_parameters
            )
            frozen_items.append(
                ResearchFrozenConfiguration(
                    native_configuration_hash=_digest(
                        getattr(item, "sha256", None), "configuration hash"
                    ),
                    factory_configuration_hash=canonical_sha256(factory_parameters),
                    parameters=factory_parameters,
                    architecture_id=architecture_id,
                    architecture_fingerprint=architecture_fingerprint,
                )
            )
        frozen = tuple(frozen_items)
        hashes = tuple(item.native_configuration_hash for item in frozen)
        if not frozen or len(set(hashes)) != len(hashes):
            raise CompatibilityError(
                "Research Runner frozen configurations are empty or duplicated"
            )
        return frozen

    def frozen_configuration_hashes(self, experiment_id: str) -> tuple[str, ...]:
        """Return the exact configuration hashes frozen in the native plan."""

        return tuple(
            item.native_configuration_hash for item in self.frozen_configurations(experiment_id)
        )

    def activate_stage(
        self,
        experiment_id: str,
        *,
        role: str,
        candidate_configuration_hashes: tuple[str, ...],
        activated_at: datetime | None = None,
    ) -> ResearchStageActivation:
        """Activate a canonical validation/test/OOS stage for frozen candidates."""

        native = self._prepared_experiment(experiment_id)
        candidates = _candidate_hashes(candidate_configuration_hashes)
        try:
            from modular_research_runner import StageRole

            native_role = StageRole(_nonblank(role, "stage role").lower())
        except (ImportError, ValueError) as exc:
            raise CompatibilityError(f"unsupported Research Runner stage role: {role}") from exc
        timestamp = (activated_at or datetime.now(UTC)).astimezone(UTC)
        run_ids = self._runner.activate_stage(
            native,
            role=native_role,
            candidate_configuration_hashes=candidates,
            activated_at=timestamp,
        )
        normalized = _run_id_tuple(run_ids, "stage activation")
        return ResearchStageActivation(
            experiment_id=experiment_id,
            target_role=native_role.value,
            candidate_configuration_hashes=candidates,
            run_ids=normalized,
            activated_at=timestamp,
        )

    def activate_optimization_stage(
        self,
        experiment_id: str,
        *,
        stage_index: int,
        candidate_configuration_hashes: tuple[str, ...],
        activated_at: datetime | None = None,
    ) -> ResearchStageActivation:
        """Promote frozen candidates into a later staged-search phase."""

        if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 1:
            raise CompatibilityError("optimization stage_index must be a positive int")
        native = self._prepared_experiment(experiment_id)
        candidates = _candidate_hashes(candidate_configuration_hashes)
        timestamp = (activated_at or datetime.now(UTC)).astimezone(UTC)
        run_ids = self._runner.activate_optimization_stage(
            native,
            stage_index=stage_index,
            candidate_configuration_hashes=candidates,
            activated_at=timestamp,
        )
        normalized = _run_id_tuple(run_ids, "optimization stage activation")
        return ResearchStageActivation(
            experiment_id=experiment_id,
            target_role=f"optimization_{stage_index:03d}",
            candidate_configuration_hashes=candidates,
            run_ids=normalized,
            activated_at=timestamp,
            optimization_stage_index=stage_index,
        )

    def authorize_final_test(
        self,
        experiment_id: str,
        *,
        candidate_configuration_hashes: tuple[str, ...],
        issued_at: datetime | None = None,
    ) -> ResearchFinalTestAuthorization:
        native = self._prepared_experiment(experiment_id)
        candidates = tuple(sorted(set(candidate_configuration_hashes)))
        if not candidates:
            raise CompatibilityError("final-test candidates cannot be empty")
        for candidate in candidates:
            _digest(candidate, "candidate configuration hash")
        grant = self._runner.authorize_final_test(
            native,
            candidate_configuration_hashes=candidates,
            issued_at=issued_at,
        )
        return _final_test_authorization(grant)

    def activate_final_test(
        self,
        experiment_id: str,
        *,
        grant_id: str,
        activated_at: datetime | None = None,
    ) -> ResearchFinalTestActivation:
        native = self._prepared_experiment(experiment_id)
        grant_id = _nonblank(grant_id, "grant_id")
        run_ids = self._runner.activate_final_test(
            native, grant_id=grant_id, activated_at=activated_at
        )
        if not isinstance(run_ids, tuple):
            raise CompatibilityError("Research Runner final-test activation must return a tuple")
        normalized = tuple(_nonblank(item, "final-test run_id") for item in run_ids)
        stored = _final_test_authorization(self._repository.get_final_test_grant(grant_id))
        if stored.experiment_id != experiment_id or stored.grant_id != grant_id:
            raise CompatibilityError("Research Runner returned a mismatched final-test grant")
        return ResearchFinalTestActivation(authorization=stored, run_ids=normalized)

    def _prepared_experiment(self, experiment_id: str) -> Any:
        experiment_id = _nonblank(experiment_id, "experiment_id")
        native = self._prepared.get(experiment_id)
        if native is None:
            raise CompatibilityError(
                "Research Runner experiment is not prepared in this process; restore submission first"
            )
        return native

    def publish_registry_packages(
        self,
        experiment_id: str,
        *,
        sink: Any,
        created_at: datetime,
    ) -> tuple[Any, ...]:
        """Publish successful native runs through Research Runner's package API."""

        native = self._prepared_experiment(experiment_id)
        publisher = getattr(self._runner, "publish_registry_packages", None)
        if not callable(publisher):
            raise CompatibilityError("Research Runner does not expose registry package publication")
        timestamp = _utc_datetime(created_at, "registry publication created_at")
        references = publisher(native, sink=sink, created_at=timestamp)
        if not isinstance(references, tuple):
            raise CompatibilityError("Research Runner registry publication must return a tuple")
        return references

    def publication_lineage(self, experiment_id: str) -> Mapping[str, Any]:
        """Return immutable, validated lineage needed by Results Aggregator intake."""

        native = self._prepared_experiment(experiment_id)
        specification = native.specification
        manifest = native.manifest
        environment = manifest.environment
        dependency_lock = getattr(environment, "dependency_lock_sha256", None)
        configurations = {
            item.native_configuration_hash: item
            for item in self.frozen_configurations(experiment_id)
        }
        if len(configurations) != len(self.frozen_configurations(experiment_id)):
            raise CompatibilityError(
                "Research Runner publication lineage contains duplicate configurations"
            )
        git = manifest.git
        return MappingProxyType(
            {
                "experiment_id": _nonblank(manifest.experiment_id, "experiment_id"),
                "research_id": _nonblank(manifest.research_id, "research_id"),
                "parent_experiment_id": manifest.parent_experiment_id,
                "title": _nonblank(specification.title, "research title"),
                "description": str(specification.description),
                "strategy_id": _nonblank(manifest.strategy_id, "strategy_id"),
                "strategy_version": _nonblank(manifest.strategy_version, "strategy_version"),
                "module_versions": MappingProxyType(dict(sorted(manifest.module_versions.items()))),
                "search_method": _nonblank(
                    getattr(specification.search_method, "value", specification.search_method),
                    "search_method",
                ),
                "split_policy": _nonblank(
                    getattr(
                        specification.split_policy.mode, "value", specification.split_policy.mode
                    ),
                    "split_policy",
                ),
                "commit_sha": _nonblank(git.commit_sha, "commit_sha"),
                "branch": _nonblank(git.branch, "branch"),
                "working_tree_status": "CLEAN" if git.clean else "DIRTY",
                "diff_hash": git.diff_sha256,
                "dependency_lock_hash": dependency_lock,
                "started_at": _utc_datetime(manifest.created_at, "manifest created_at"),
                "configurations": MappingProxyType(configurations),
            }
        )

    def status(self, experiment_id: str) -> ResearchExperimentSnapshot:
        experiment_id = _nonblank(experiment_id, "experiment_id")
        status = self._repository.experiment_status(experiment_id)
        counts = self._repository.counts(experiment_id)
        if not isinstance(counts, Mapping):
            raise CompatibilityError("Research Runner counts must be a mapping")
        normalized_counts: dict[str, int] = {}
        for key, value in counts.items():
            name = getattr(key, "value", key)
            if not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, int):
                raise CompatibilityError("Research Runner returned invalid status counts")
            normalized_counts[name.lower()] = value
        runs = self._repository.list_runs(experiment_id)
        if not isinstance(runs, tuple):
            raise CompatibilityError("Research Runner list_runs must return a tuple")
        run_ids = tuple(_nonblank(getattr(run, "run_id", None), "research run_id") for run in runs)
        return ResearchExperimentSnapshot(
            experiment_id=experiment_id,
            status=_nonblank(getattr(status, "value", status), "experiment status").lower(),
            run_status_counts=MappingProxyType(dict(sorted(normalized_counts.items()))),
            run_ids=run_ids,
            has_nonterminal_runs=_strict_bool(
                self._repository.has_nonterminal_runs(experiment_id),
                "Research Runner has_nonterminal_runs",
            ),
        )

    def execute(self, experiment_id: str) -> ResearchControlResult:
        experiment_id = _nonblank(experiment_id, "experiment_id")
        owned_token: Any | None = None
        try:
            from modular_research_runner import CancellationToken

            if experiment_id in self._cancellations:
                return ResearchControlResult(
                    snapshot=None,
                    failure=StructuredExternalFailure(
                        operation="execute",
                        error_type="DuplicateExecution",
                        reason_code="EXPERIMENT_ALREADY_EXECUTING",
                        message="experiment execution is already active",
                        retryable=True,
                    ),
                )
            owned_token = CancellationToken()
            self._cancellations[experiment_id] = owned_token
            self._runner.execute(experiment_id, cancellation=owned_token)
            return ResearchControlResult(snapshot=self.status(experiment_id), failure=None)
        except Exception as exc:
            return ResearchControlResult(
                snapshot=None,
                failure=StructuredExternalFailure(
                    operation="execute",
                    error_type=type(exc).__name__,
                    reason_code="RESEARCH_RUNNER_EXECUTION_FAILED",
                    message=str(exc),
                    retryable=not isinstance(exc, (CompatibilityError, ValueError, TypeError)),
                ),
            )
        finally:
            if owned_token is not None and self._cancellations.get(experiment_id) is owned_token:
                self._cancellations.pop(experiment_id, None)

    def cancel(
        self, experiment_id: str, *, at: datetime | None = None
    ) -> ResearchExperimentSnapshot:
        experiment_id = _nonblank(experiment_id, "experiment_id")
        token = self._cancellations.get(experiment_id)
        if token is not None:
            token.cancel()
        canceled = self._repository.cancel_pending(
            experiment_id, at=(at or datetime.now(UTC)).astimezone(UTC)
        )
        if isinstance(canceled, bool) or not isinstance(canceled, int) or canceled < 0:
            raise CompatibilityError("Research Runner cancel_pending returned invalid count")
        return self.status(experiment_id)

    def resume(self, experiment_id: str) -> ResearchControlResult:
        snapshot = self.status(experiment_id)
        if snapshot.status in {"completed", "completed_with_failures"}:
            return ResearchControlResult(snapshot=snapshot, failure=None)
        if snapshot.status in {"canceled", "corrupted"}:
            return ResearchControlResult(
                snapshot=None,
                failure=StructuredExternalFailure(
                    operation="resume",
                    error_type="IncompatibleExperimentState",
                    reason_code="EXPERIMENT_NOT_RESUMABLE",
                    message=f"experiment status is {snapshot.status}",
                    retryable=False,
                ),
            )
        return self.execute(experiment_id)


def _runtime_blueprint(
    architecture: StrategyArchitecture,
) -> tuple[Mapping[str, Any], ...]:
    """Freeze trusted runtime construction data into every Research Runner job."""

    return tuple(
        MappingProxyType(
            {
                "runtime_component_id": module.runtime_component_id,
                "runtime_component_version": module.runtime_component_version,
                "import_path": module.import_path,
                "fixed_parameters": dict(module.fixed_parameters),
                "parameter_schema": dict(module.parameter_schema),
                "api_version": module.api_version,
                "state_schema_version": module.state_schema_version,
                "source_commit_sha": module.source_commit_sha,
            }
        )
        for module in architecture.runtime_modules
    )


def _strategy_parameters_from_native(
    configuration: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Project Research Runner provenance fields out of native candidate identity.

    New Factory plans carry a code-owned ``runtime_modules`` blueprint. Only
    runtime-qualified values are strategy parameters; architecture/run metadata
    remains separate provenance. Older plans without a blueprint preserve their
    complete mapping for backward-compatible inspection.
    """

    raw_blueprint = configuration.get("runtime_modules")
    if not isinstance(raw_blueprint, (tuple, list)):
        return MappingProxyType(dict(configuration))
    component_ids: set[str] = set()
    for index, item in enumerate(raw_blueprint):
        if not isinstance(item, Mapping):
            raise CompatibilityError(f"runtime_modules[{index}] must be a mapping")
        component_id = _nonblank(
            item.get("runtime_component_id"),
            f"runtime_modules[{index}].runtime_component_id",
        )
        if component_id in component_ids:
            raise CompatibilityError("runtime_modules contains duplicate runtime component IDs")
        component_ids.add(component_id)
    projected = {
        str(name): value
        for name, value in configuration.items()
        if isinstance(name, str) and "." in name and name.split(".", 1)[0] in component_ids
    }
    return MappingProxyType(dict(sorted(projected.items())))


def _native_architecture_identity(
    configuration: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    architecture_id = configuration.get("architecture_id")
    architecture_fingerprint = configuration.get("architecture_fingerprint")
    if architecture_id is None and architecture_fingerprint is None:
        return None, None
    return (
        _nonblank(architecture_id, "architecture_id"),
        _digest(architecture_fingerprint, "architecture_fingerprint"),
    )


def _runtime_parameter_projection(
    architecture: StrategyArchitecture,
    configuration: Mapping[str, Any],
) -> Mapping[str, Any]:
    expected = {
        f"{module.runtime_component_id}.{parameter_name}"
        for module in architecture.runtime_modules
        for parameter_name in module.parameter_schema
    }
    return MappingProxyType(
        {name: configuration[name] for name in sorted(expected) if name in configuration}
    )


class ResearchRunnerAdapter:
    """Translate Factory plans into the existing Research Runner v0.3 contracts."""

    def __init__(self, *, output_root: str | Path) -> None:
        self._output_root = Path(output_root)

    def build_plan(
        self,
        *,
        request: StrategyFactoryRequest,
        factory_run_id: str,
        manifest_id: str,
        stage: FactoryStage,
        architecture: StrategyArchitecture,
        parameter_space: ParameterSpace,
        split: ResearchSplitPlan,
        search_method: str = "random",
        experiment_count: int | None = None,
    ) -> PreparedResearchPlan:
        try:
            import modular_research_runner as research
        except ImportError as exc:
            raise ExternalComponentUnavailableError("Research Runner is unavailable") from exc

        dimensions = tuple(
            _research_parameter_dimension(research, name, values)
            for name, values in parameter_space.dimensions.items()
        )
        constraints = tuple(
            _research_constraint(research, item) for item in parameter_space.constraints
        )
        native_space = research.SearchSpace(
            dimensions=dimensions,
            constraints=constraints,
            maximum_combinations=min(
                parameter_space.maximum_combinations,
                experiment_count or parameter_space.maximum_combinations,
            ),
        )
        lineage = research.DatasetLineage(
            dataset_id=request.dataset.dataset_id,
            source=request.dataset.source,
            market_type=request.dataset.market_type.value,
            instrument=request.dataset.symbol,
            timeframe=request.dataset.base_timeframe,
            period=research.TimeRange(request.dataset.period.start, request.dataset.period.end),
            row_count=request.dataset.row_count,
            timezone=request.dataset.timezone,
            file_format=request.dataset.file_format,
            file_size=_file_size(request.dataset.uri),
            sha256=request.dataset.sha256,
            preprocessing_version="strategy-factory-v1",
            aggregation_version="source-declared-v1",
            metadata_version="dataset-descriptor-v1",
            funding_data_sha256=request.dataset.funding_sha256,
            quality_status=research.DatasetQualityStatus.VALID,
        )
        method = research.SearchMethod(search_method)
        native_split = research.ChronologicalSplitPolicy(
            mode=research.SplitMode.FIXED,
            train=research.TimeRange(split.train.start, split.train.end),
            validation=research.TimeRange(split.validation.start, split.validation.end),
            test=research.TimeRange(split.test.start, split.test.end),
            final_untouched_test=research.TimeRange(split.holdout.start, split.holdout.end),
            gap_seconds=split.gap_seconds,
        )
        staged = None
        if method is research.SearchMethod.STAGED:
            population = native_space.maximum_combinations
            if population < 3:
                raise CompatibilityError("staged search requires at least three configurations")
            first = min(64, population - 1)
            second = max(1, min(16, first - 1))
            staged = research.StagedSearchPolicy(
                train_fractions=(0.25, 0.5, 1.0),
                promotion_counts=(first, second),
            )
        module_versions = {item.module_id: item.version for item in architecture.modules}
        specification = research.ResearchSpecification(
            research_id=f"research-{factory_run_id}-{stage.value}",
            title=f"Strategy Factory {stage.value} for {request.symbol}",
            description="Generated by Strategy Factory from a declarative architecture.",
            strategy_id=architecture.architecture_id,
            strategy_version=architecture.grammar_version,
            module_versions=module_versions,
            base_configuration={
                "architecture_id": architecture.architecture_id,
                "architecture_fingerprint": architecture.fingerprint,
                "modules": [item.key for item in architecture.modules],
                "runtime_modules": _runtime_blueprint(architecture),
                "parameters": dict(architecture.parameters),
                "factory_run_id": factory_run_id,
                "factory_manifest_id": manifest_id,
            },
            search_method=method,
            search_space=native_space,
            datasets=(lineage,),
            instruments=(request.symbol,),
            market_type=request.market_type.value,
            timeframes=request.allowed_timeframes,
            date_range=research.TimeRange(split.train.start, split.holdout.end),
            split_policy=native_split,
            staged_search_policy=staged,
            validation_policy={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
                "holdout_isolation": "one_time_final_untouched_grant",
            },
            execution_cost_scenarios=tuple(request.objectives.get("cost_scenarios", ())),
            robustness_scenarios=tuple(request.objectives.get("robustness_scenarios", ())),
            seeds=(request.random_seed,),
            resource_limits={
                "maximum_memory_bytes": request.budget.maximum_memory_bytes,
                "maximum_duration_seconds": request.budget.maximum_duration_seconds,
                "maximum_runs": experiment_count or parameter_space.maximum_combinations,
                "maximum_workers": request.budget.maximum_cpu_workers,
                **(
                    {"random_sample_count": native_space.maximum_combinations}
                    if method is research.SearchMethod.RANDOM
                    else {}
                ),
            },
            parallelism=request.budget.maximum_parallel_tasks,
            retry_policy={
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            },
            resume_policy={
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            },
            ranking_objective=dict(request.objectives),
            promotion_criteria={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
            },
            artifact_retention={"retain_failures": True, "retain_all_manifests": True},
        )
        experiment_id = (
            f"exp-{canonical_sha256((factory_run_id, stage.value, specification.fingerprint))[:24]}"
        )
        native_plan = research.ResearchPlanBuilder(self._output_root).build(
            experiment_id=experiment_id,
            specification=specification,
        )
        spec_mapping = json.loads(canonical_json_bytes(specification))
        factory_resource_limits = {
            "native_experiment_id": native_plan.experiment_id,
            "job_count": len(native_plan.jobs),
            "parallelism": request.budget.maximum_parallel_tasks,
        }
        plan_identity = canonical_sha256(
            {
                "factory_run_id": factory_run_id,
                "stage": stage.value,
                "architecture_fingerprint": architecture.fingerprint,
                "parameter_space_fingerprint": canonical_sha256(parameter_space),
                "dataset_fingerprint": request.dataset.fingerprint,
                "research_specification_fingerprint": specification.fingerprint,
                "resource_limits": factory_resource_limits,
                "deterministic_seed": request.random_seed,
                "manifest_id": manifest_id,
            }
        )
        factory_plan = ExperimentPlan(
            plan_id=f"sf-plan-{plan_identity[:24]}",
            factory_run_id=factory_run_id,
            stage=stage,
            architecture=architecture,
            parameter_space=parameter_space,
            dataset_fingerprint=request.dataset.fingerprint,
            research_specification=spec_mapping,
            resource_limits=factory_resource_limits,
            deterministic_seed=request.random_seed,
            manifest_id=manifest_id,
        )
        return PreparedResearchPlan(factory_plan, specification, native_plan, request.created_at)

    def restore_plan(
        self,
        *,
        request: StrategyFactoryRequest,
        frozen_plan: ExperimentPlan,
        split: ResearchSplitPlan,
        search_method: str,
        experiment_count: int,
    ) -> PreparedResearchPlan:
        """Rebuild a preview plan and require byte-stable Factory identity."""

        rebuilt = self.build_plan(
            request=request,
            factory_run_id=frozen_plan.factory_run_id,
            manifest_id=frozen_plan.manifest_id,
            stage=frozen_plan.stage,
            architecture=frozen_plan.architecture,
            parameter_space=frozen_plan.parameter_space,
            split=split,
            search_method=search_method,
            experiment_count=experiment_count,
        )
        if canonical_sha256(rebuilt.factory_plan) != canonical_sha256(frozen_plan):
            raise CompatibilityError(
                "rebuilt Research Runner preview differs from the frozen Factory plan"
            )
        return rebuilt

    def build_walk_forward_plan(
        self,
        *,
        request: StrategyFactoryRequest,
        factory_run_id: str,
        manifest_id: str,
        candidate: CandidateStrategy,
        split: ResearchSplitPlan,
    ) -> PreparedResearchPlan:
        """Build a single-candidate walk-forward experiment with untouched holdout."""

        try:
            import modular_research_runner as research
        except ImportError as exc:
            raise ExternalComponentUnavailableError("Research Runner is unavailable") from exc
        architecture = candidate.architecture
        lineage = research.DatasetLineage(
            dataset_id=request.dataset.dataset_id,
            source=request.dataset.source,
            market_type=request.dataset.market_type.value,
            instrument=request.dataset.symbol,
            timeframe=request.dataset.base_timeframe,
            period=research.TimeRange(
                request.dataset.period.start,
                request.dataset.period.end,
            ),
            row_count=request.dataset.row_count,
            timezone=request.dataset.timezone,
            file_format=request.dataset.file_format,
            file_size=_file_size(request.dataset.uri),
            sha256=request.dataset.sha256,
            preprocessing_version="strategy-factory-v1",
            aggregation_version="source-declared-v1",
            metadata_version="dataset-descriptor-v1",
            funding_data_sha256=request.dataset.funding_sha256,
            quality_status=research.DatasetQualityStatus.VALID,
        )
        native_split = research.ChronologicalSplitPolicy(
            mode=research.SplitMode.FIXED,
            train=research.TimeRange(split.train.start, split.train.end),
            validation=research.TimeRange(split.validation.start, split.validation.end),
            test=research.TimeRange(split.test.start, split.test.end),
            final_untouched_test=research.TimeRange(split.holdout.start, split.holdout.end),
            gap_seconds=split.gap_seconds,
        )
        walk_forward = _walk_forward_policy(research, request, split)
        native_space = research.SearchSpace(
            dimensions=(
                research.ParameterDimension(
                    name="factory_candidate_hash",
                    kind=research.ParameterKind.CATEGORICAL,
                    values=(candidate.configuration_hash,),
                ),
            ),
            maximum_combinations=1,
        )
        module_versions = {item.module_id: item.version for item in architecture.modules}
        specification = research.ResearchSpecification(
            research_id=(
                f"research-{factory_run_id}-{FactoryStage.WALK_FORWARD.value}-"
                f"{candidate.candidate_id}"
            ),
            title=f"Strategy Factory walk-forward for {candidate.candidate_id}",
            description=(
                "Single frozen candidate walk-forward validation generated by "
                "Strategy Factory. Final untouched holdout remains gated."
            ),
            strategy_id=architecture.architecture_id,
            strategy_version=architecture.grammar_version,
            module_versions=module_versions,
            base_configuration={
                "architecture_id": architecture.architecture_id,
                "architecture_fingerprint": architecture.fingerprint,
                "modules": [item.key for item in architecture.modules],
                "runtime_modules": _runtime_blueprint(architecture),
                "factory_run_id": factory_run_id,
                "factory_manifest_id": manifest_id,
                **dict(candidate.parameters),
            },
            search_method=research.SearchMethod.SINGLE,
            search_space=native_space,
            datasets=(lineage,),
            instruments=(request.symbol,),
            market_type=request.market_type.value,
            timeframes=request.allowed_timeframes,
            date_range=research.TimeRange(split.train.start, split.holdout.end),
            split_policy=native_split,
            walk_forward_policy=walk_forward,
            validation_policy={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
                "candidate_configuration_hash": candidate.configuration_hash,
                "holdout_isolation": "one_time_final_untouched_grant",
            },
            seeds=(request.random_seed,),
            resource_limits={
                "maximum_memory_bytes": request.budget.maximum_memory_bytes,
                "maximum_duration_seconds": request.budget.maximum_duration_seconds,
                "maximum_runs": request.budget.stage_experiment_limits.get(
                    FactoryStage.WALK_FORWARD.value,
                    request.budget.maximum_experiments,
                ),
                "maximum_workers": request.budget.maximum_cpu_workers,
            },
            parallelism=request.budget.maximum_parallel_tasks,
            retry_policy={
                "max_attempts": 2,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "maximum_backoff_seconds": 30.0,
                "retryable_categories": ("TRANSIENT", "RESOURCE"),
            },
            resume_policy={
                "lease_seconds": 900,
                "heartbeat_seconds": 60,
                "recover_expired_leases": True,
                "verify_success_artifacts": True,
            },
            ranking_objective=dict(request.objectives),
            promotion_criteria={
                "minimum_trade_count": request.minimum_trade_count,
                "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
            },
            artifact_retention={
                "retain_failures": True,
                "retain_all_manifests": True,
            },
        )
        preview_id = (
            "exp-"
            + canonical_sha256(
                (
                    factory_run_id,
                    FactoryStage.WALK_FORWARD.value,
                    candidate.candidate_id,
                    specification.fingerprint,
                )
            )[:24]
        )
        builder = research.ResearchPlanBuilder(self._output_root)
        native_plan = builder.build(
            experiment_id=preview_id,
            specification=specification,
        )
        native_configuration = native_plan.configurations[0]
        native_configuration_hash = native_configuration.sha256
        projected_parameters = _runtime_parameter_projection(
            architecture,
            native_configuration.parameters,
        )
        candidate_parameters = _runtime_parameter_projection(
            architecture,
            candidate.parameters,
        )
        if projected_parameters != candidate_parameters:
            raise CompatibilityError(
                "walk-forward native strategy parameters differ from frozen candidate"
            )
        activation_jobs: list[Any] = []
        roles: list[str] = []
        for role in (research.StageRole.VALIDATION, research.StageRole.OOS):
            if any(item.role is role for item in native_plan.stages):
                roles.append(role.value)
                activation_jobs.extend(
                    builder.build_stage_jobs(
                        plan=native_plan,
                        specification=specification,
                        role=role,
                        candidate_configuration_hashes=(native_configuration_hash,),
                    )
                )
        if roles != ["validation", "oos"]:
            raise CompatibilityError("walk-forward plan must expose validation and OOS stages")
        final_jobs = builder.build_final_jobs(
            plan=native_plan,
            specification=specification,
            candidate_configuration_hashes=(native_configuration_hash,),
        )
        parameter_space = ParameterSpace(
            space_id=f"sf-wf-space-{candidate.configuration_hash[:24]}",
            architecture_id=architecture.architecture_id,
            dimensions={
                "factory_candidate_hash": (candidate.configuration_hash,),
            },
            maximum_combinations=1,
        )
        specification_mapping = json.loads(canonical_json_bytes(specification))
        factory_plan = ExperimentPlan(
            plan_id=f"sf-plan-{native_plan.fingerprint[:24]}",
            factory_run_id=factory_run_id,
            stage=FactoryStage.WALK_FORWARD,
            architecture=architecture,
            parameter_space=parameter_space,
            dataset_fingerprint=request.dataset.fingerprint,
            research_specification=specification_mapping,
            resource_limits={
                "native_experiment_id": native_plan.experiment_id,
                "initial_job_count": len(native_plan.jobs),
                "activation_job_count": len(activation_jobs),
                "planned_total_jobs": len(native_plan.jobs) + len(activation_jobs),
                "final_job_count": len(final_jobs),
                "activation_roles": tuple(roles),
                "parallelism": request.budget.maximum_parallel_tasks,
                "source_candidate_id": candidate.candidate_id,
            },
            deterministic_seed=request.random_seed,
            manifest_id=manifest_id,
        )
        return PreparedResearchPlan(
            factory_plan=factory_plan,
            native_specification=specification,
            native_plan=native_plan,
            created_at=request.created_at,
        )


@dataclass(frozen=True, slots=True)
class AggregatedCandidate:
    configuration_id: str
    configuration_hash: str
    producer_configuration_hash: str | None
    canonical_configuration: Mapping[str, Any]
    run_ids: tuple[str, ...]
    evaluation: CandidateEvaluation

    def __post_init__(self) -> None:
        _nonblank(self.configuration_id, "configuration_id")
        _digest(self.configuration_hash, "configuration_hash")
        if self.producer_configuration_hash is not None:
            _digest(
                self.producer_configuration_hash,
                "producer_configuration_hash",
            )
        if not isinstance(self.canonical_configuration, Mapping):
            raise CompatibilityError(
                "aggregated candidate canonical_configuration must be a mapping"
            )
        object.__setattr__(
            self,
            "canonical_configuration",
            MappingProxyType(dict(self.canonical_configuration)),
        )


class ResultsAggregatorAdapter:
    """Read candidate evidence from the canonical Results Aggregator registry."""

    def __init__(self, registry_path: str | Path) -> None:
        try:
            from modular_results_registry import SQLiteRegistry
            from modular_results_registry.query import RegistryQueryAPI
        except ImportError as exc:
            raise ExternalComponentUnavailableError("Results Aggregator is unavailable") from exc
        self._registry = SQLiteRegistry(registry_path)
        self._query = RegistryQueryAPI(self._registry)

    def load_candidate(
        self, configuration_id: str, *, generated_at: datetime
    ) -> AggregatedCandidate:
        configuration = self._query.configuration(configuration_id)
        if configuration is None:
            raise CompatibilityError(f"unknown configuration: {configuration_id}")
        runs = tuple(
            self._paginate(
                lambda offset: self._query.runs(
                    configuration_id=configuration_id, limit=1_000, offset=offset
                )
            )
        )
        metrics: dict[str, Any] = {}
        flags: set[str] = set()
        for run in runs:
            for snapshot in self._paginate(
                lambda offset, run_id=run.run_id: self._query.metrics_for_run(
                    run_id, limit=1_000, offset=offset
                )
            ):
                key = f"{snapshot.split}.{snapshot.scenario}.{snapshot.metric_name}"
                metrics[key] = snapshot.value
            flags.update(run.warnings)
            flags.update(run.errors)
        score_components = _derive_score_components(metrics)
        metrics = {
            **metrics,
            **{
                key: value for key, value in score_components.items() if key != "complexity_penalty"
            },
        }
        passed = not any(item.startswith("ERROR") for item in flags)
        evaluation_id = f"candidate-evaluation-{canonical_sha256((configuration_id, metrics))[:24]}"
        evaluation = CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=configuration_id,
            metrics=metrics,
            constraints={"registry_evidence_clean": passed},
            pareto_front=0,
            instability_flags=tuple(sorted(flags)),
            complexity=Decimal(str(score_components.get("complexity_penalty", 0))),
            evidence_references=tuple(
                artifact.artifact_id
                for run in runs
                for artifact in self._query.artifacts_for_run(run.run_id)
            ),
        )
        return AggregatedCandidate(
            configuration_id=configuration_id,
            configuration_hash=configuration.configuration_hash,
            producer_configuration_hash=configuration.producer_configuration_hash,
            canonical_configuration=configuration.canonical_configuration,
            run_ids=tuple(run.run_id for run in runs),
            evaluation=evaluation,
        )

    def candidates_for_experiment(
        self,
        experiment_id: str,
        *,
        generated_at: datetime,
    ) -> tuple[AggregatedCandidate, ...]:
        """Load the canonical candidate population selected by the registry."""

        experiment_id = _nonblank(experiment_id, "experiment_id")
        configurations = self._query.candidates(experiment_id)
        if not isinstance(configurations, tuple):
            raise CompatibilityError("Results Aggregator candidates() must return a tuple")
        identities = tuple(item.configuration_id for item in configurations)
        if len(identities) != len(set(identities)):
            raise CompatibilityError("Results Aggregator returned duplicate candidate identities")
        return tuple(
            self.load_candidate(configuration_id, generated_at=generated_at)
            for configuration_id in sorted(identities)
        )

    @staticmethod
    def _paginate(fetch_page: Any) -> Any:
        offset = 0
        visited: set[int] = set()
        while True:
            if offset in visited:
                raise CompatibilityError("Results Aggregator pagination cycle detected")
            visited.add(offset)
            page = fetch_page(offset)
            items = getattr(page, "items", None)
            next_offset = getattr(page, "next_offset", None)
            if not isinstance(items, tuple):
                raise CompatibilityError("Results Aggregator returned an invalid page")
            yield from items
            if next_offset is None:
                return
            if (
                isinstance(next_offset, bool)
                or not isinstance(next_offset, int)
                or next_offset <= offset
            ):
                raise CompatibilityError("Results Aggregator returned an invalid next_offset")
            offset = next_offset

    def close(self) -> None:
        self._registry.close()


@runtime_checkable
class StrategyPackageSourcePort(Protocol):
    """Read-only source for an already versioned Strategy Package."""

    def load(self, strategy_id: str) -> StrategyPackage | Mapping[str, Any]: ...


class UnavailableStrategyPackageSourcePort:
    """Fail closed when no canonical package repository is configured."""

    def load(self, strategy_id: str) -> StrategyPackage | Mapping[str, Any]:
        del strategy_id
        raise ExternalComponentUnavailableError(
            "no canonical Strategy Package source is configured"
        )


@runtime_checkable
class StrategyAuditPort(Protocol):
    """Versioned external boundary; implementation is intentionally not owned here."""

    contract_version: str

    def audit(self, evidence_package_reference: str) -> Mapping[str, Any]:
        """Return a signed/immutable audit decision for a prepared evidence package."""


class UnavailableStrategyAuditPort:
    """Fail-closed placeholder for the currently absent repository component."""

    contract_version = "strategy-audit-port-v1"

    def audit(self, evidence_package_reference: str) -> Mapping[str, Any]:
        del evidence_package_reference
        raise ExternalComponentUnavailableError(
            "Strategy Audit & Validation is not implemented in the current v0.6 repository"
        )


def _submission_from_native(factory_plan_id: str, native: Any) -> ResearchSubmission:
    experiment_id = _nonblank(
        getattr(native.manifest, "experiment_id", None),
        "prepared experiment_id",
    )
    return ResearchSubmission(
        factory_plan_id=factory_plan_id,
        experiment_id=experiment_id,
        specification_fingerprint=_fingerprint(native.specification, "research specification"),
        plan_fingerprint=_fingerprint(native.plan, "research plan"),
        manifest_fingerprint=_fingerprint(native.manifest, "research manifest"),
    )


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"{name} must be a non-blank trimmed string")
    return value


def _research_plan_semantic_fingerprint(plan: object) -> str:
    """Compare plan semantics while excluding IDs/seeds derived from experiment identity."""

    split = getattr(plan, "split", None)
    configurations = getattr(plan, "configurations", None)
    scenarios = getattr(plan, "scenarios", None)
    stages = getattr(plan, "stages", None)
    final_stages = getattr(plan, "final_stages", None)
    jobs = getattr(plan, "jobs", None)
    optimization_stages = getattr(plan, "optimization_stages", ())
    if any(
        value is None for value in (split, configurations, scenarios, stages, final_stages, jobs)
    ):
        return _fingerprint(plan, "research plan")
    assert split is not None
    assert configurations is not None
    assert scenarios is not None
    assert stages is not None
    assert final_stages is not None
    assert jobs is not None

    def stage_value(stage: object) -> dict[str, object]:
        role = getattr(stage, "role", None)
        return {
            "role": getattr(role, "value", role),
            "split_id": getattr(stage, "split_id", None),
            "dataset_id": getattr(stage, "dataset_id", None),
            "period": getattr(stage, "period", None),
            "window_id": getattr(stage, "window_id", None),
            "stage_index": getattr(stage, "stage_index", None),
            "maximum_candidates": getattr(stage, "maximum_candidates", None),
        }

    payload = {
        "split": _fingerprint(split, "research split"),
        "configurations": tuple(
            sorted(
                _digest(getattr(item, "sha256", None), "configuration hash")
                for item in configurations
            )
        ),
        "scenarios": tuple(sorted(_fingerprint(item, "research scenario") for item in scenarios)),
        "stages": _canonical_sorted(stage_value(item) for item in stages),
        "final_stages": _canonical_sorted(stage_value(item) for item in final_stages),
        "optimization_stages": _canonical_sorted(stage_value(item) for item in optimization_stages),
        "jobs": _canonical_sorted(
            {
                "stage_id": getattr(item, "stage_id", None),
                "split_id": getattr(item, "split_id", None),
                "dataset_id": getattr(item, "dataset_id", None),
                "scenario_id": getattr(item, "scenario_id", None),
                "parameters": getattr(item, "parameters", None),
                "stage_role": getattr(item, "stage_role", None),
                "period": getattr(item, "period", None),
                "repetition_index": getattr(item, "repetition_index", None),
                "metadata": getattr(item, "metadata", None),
            }
            for item in jobs
        ),
    }
    return canonical_sha256(payload)


def _native_artifact_reference(
    research: Any,
    artifact_store: Any,
    relative_path: str,
) -> Any:
    root = getattr(artifact_store, "root", None)
    if not isinstance(root, Path):
        raise CompatibilityError("Research Runner artifact store root must be a pathlib.Path")
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise CompatibilityError("unsafe Research Runner artifact path")
    target = root.joinpath(*relative.parts)
    if target.is_symlink() or not target.is_file():
        raise CompatibilityError(f"Research Runner artifact is missing or unsafe: {relative_path}")
    payload = target.read_bytes()
    reference = research.ArtifactReference(
        relative_path=relative_path,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    # Let the native store repeat its own path, size, and checksum validation.
    artifact_store.read_json(reference)
    return reference


def _decode_native_manifest(research: Any, value: object) -> Any:
    mapping = _native_mapping(value, "Research Runner manifest")
    allowed = {
        "experiment_id",
        "research_id",
        "parent_experiment_id",
        "specification_sha256",
        "search_space_sha256",
        "configuration_hashes",
        "dataset_hashes",
        "split",
        "strategy_id",
        "strategy_version",
        "module_versions",
        "module_api_version",
        "backtest_engine_version",
        "metric_schema_version",
        "git",
        "environment",
        "scheduler_configuration",
        "resource_limits",
        "seeds",
        "timezone",
        "created_at",
        "status",
        "schema_version",
    }
    unknown = set(mapping) - allowed
    missing = allowed - set(mapping)
    if unknown or missing:
        raise CompatibilityError(
            "Research Runner manifest fields differ from schema; "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    split_mapping = _native_mapping(mapping["split"], "manifest split")
    _require_exact_keys(
        split_mapping,
        {
            "split_id",
            "train",
            "validation",
            "test",
            "final_untouched_test",
        },
        "manifest split",
    )
    split = research.PlannedSplit(
        split_id=_nonblank(split_mapping["split_id"], "split_id"),
        train=_decode_native_time_range(research, split_mapping["train"], "train"),
        validation=_decode_native_time_range(research, split_mapping["validation"], "validation"),
        test=_decode_native_time_range(research, split_mapping["test"], "test"),
        final_untouched_test=(
            None
            if split_mapping["final_untouched_test"] is None
            else _decode_native_time_range(
                research,
                split_mapping["final_untouched_test"],
                "final_untouched_test",
            )
        ),
    )
    git_mapping = _native_mapping(mapping["git"], "manifest git")
    _require_exact_keys(
        git_mapping,
        {"branch", "commit_sha", "clean", "diff_sha256", "untracked_policy"},
        "manifest git",
    )
    environment_mapping = _native_mapping(mapping["environment"], "manifest environment")
    _require_exact_keys(
        environment_mapping,
        {"python_version", "platform", "dependency_lock_sha256"},
        "manifest environment",
    )
    configuration_hashes = _native_string_tuple(
        mapping["configuration_hashes"], "configuration_hashes"
    )
    for digest in configuration_hashes:
        _digest(digest, "manifest configuration hash")
    seeds = _native_int_tuple(mapping["seeds"], "manifest seeds")
    return research.ExperimentManifest(
        experiment_id=_nonblank(mapping["experiment_id"], "experiment_id"),
        research_id=_nonblank(mapping["research_id"], "research_id"),
        parent_experiment_id=(
            None
            if mapping["parent_experiment_id"] is None
            else _nonblank(mapping["parent_experiment_id"], "parent_experiment_id")
        ),
        specification_sha256=_digest(mapping["specification_sha256"], "specification_sha256"),
        search_space_sha256=_digest(mapping["search_space_sha256"], "search_space_sha256"),
        configuration_hashes=configuration_hashes,
        dataset_hashes=_native_digest_mapping(mapping["dataset_hashes"], "dataset_hashes"),
        split=split,
        strategy_id=_nonblank(mapping["strategy_id"], "strategy_id"),
        strategy_version=_nonblank(mapping["strategy_version"], "strategy_version"),
        module_versions=_native_string_mapping(mapping["module_versions"], "module_versions"),
        module_api_version=_nonblank(mapping["module_api_version"], "module_api_version"),
        backtest_engine_version=_nonblank(
            mapping["backtest_engine_version"], "backtest_engine_version"
        ),
        metric_schema_version=_nonblank(mapping["metric_schema_version"], "metric_schema_version"),
        git=research.GitProvenance(
            branch=_nonblank(git_mapping["branch"], "git.branch"),
            commit_sha=_nonblank(git_mapping["commit_sha"], "git.commit_sha"),
            clean=_strict_bool(git_mapping["clean"], "git.clean"),
            diff_sha256=(
                None
                if git_mapping["diff_sha256"] is None
                else _digest(git_mapping["diff_sha256"], "git.diff_sha256")
            ),
            untracked_policy=_nonblank(git_mapping["untracked_policy"], "git.untracked_policy"),
        ),
        environment=research.EnvironmentManifest(
            python_version=_nonblank(
                environment_mapping["python_version"], "environment.python_version"
            ),
            platform=_nonblank(environment_mapping["platform"], "environment.platform"),
            dependency_lock_sha256=(
                None
                if environment_mapping["dependency_lock_sha256"] is None
                else _digest(
                    environment_mapping["dependency_lock_sha256"],
                    "environment.dependency_lock_sha256",
                )
            ),
        ),
        scheduler_configuration=_native_mapping(
            mapping["scheduler_configuration"], "scheduler_configuration"
        ),
        resource_limits=_native_mapping(mapping["resource_limits"], "resource_limits"),
        seeds=seeds,
        timezone=_nonblank(mapping["timezone"], "timezone"),
        created_at=_utc_datetime(mapping["created_at"], "manifest created_at"),
        status=research.ExperimentStatus(_nonblank(mapping["status"], "manifest status")),
        schema_version=_native_int(mapping["schema_version"], "manifest schema_version"),
    )


def _decode_native_time_range(
    research: Any,
    value: object,
    name: str,
) -> Any:
    mapping = _native_mapping(value, f"manifest {name}")
    _require_exact_keys(mapping, {"start", "end"}, f"manifest {name}")
    return research.TimeRange(
        _utc_datetime(mapping["start"], f"{name}.start"),
        _utc_datetime(mapping["end"], f"{name}.end"),
    )


def _native_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CompatibilityError(f"{name} must be a string-keyed mapping")
    return value


def _require_exact_keys(
    mapping: Mapping[str, Any],
    expected: set[str],
    name: str,
) -> None:
    if set(mapping) != expected:
        raise CompatibilityError(f"{name} fields differ from the expected schema")


def _native_string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise CompatibilityError(f"{name} must be a tuple")
    return tuple(_nonblank(item, name) for item in value)


def _native_int_tuple(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, tuple):
        raise CompatibilityError(f"{name} must be a tuple")
    return tuple(_native_int(item, name) for item in value)


def _native_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompatibilityError(f"{name} must be int")
    return value


def _native_string_mapping(value: object, name: str) -> Mapping[str, str]:
    mapping = _native_mapping(value, name)
    return {
        _nonblank(key, f"{name} key"): _nonblank(item, f"{name} value")
        for key, item in mapping.items()
    }


def _native_digest_mapping(value: object, name: str) -> Mapping[str, str]:
    mapping = _native_mapping(value, name)
    return {
        _nonblank(key, f"{name} key"): _digest(item, f"{name} value")
        for key, item in mapping.items()
    }


def _canonical_sorted(values: Any) -> tuple[Any, ...]:
    return tuple(sorted(tuple(values), key=canonical_json_bytes))


def _digest(value: object, name: str) -> str:
    text = _nonblank(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CompatibilityError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompatibilityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _candidate_hashes(values: tuple[str, ...]) -> tuple[str, ...]:
    candidates = tuple(sorted(set(values)))
    if not candidates:
        raise CompatibilityError("candidate configuration hashes cannot be empty")
    for candidate in candidates:
        _digest(candidate, "candidate configuration hash")
    return candidates


def _run_id_tuple(value: Any, operation: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise CompatibilityError(f"Research Runner {operation} must return a tuple")
    normalized = tuple(_nonblank(item, f"{operation} run_id") for item in value)
    if not normalized or len(set(normalized)) != len(normalized):
        raise CompatibilityError(f"Research Runner {operation} returned empty or duplicate run IDs")
    return normalized


def _final_test_authorization(value: object) -> ResearchFinalTestAuthorization:
    candidates = getattr(value, "candidate_configuration_hashes", None)
    if not isinstance(candidates, tuple):
        raise CompatibilityError("Research Runner final-test grant candidates are invalid")
    consumed_at = getattr(value, "consumed_at", None)
    return ResearchFinalTestAuthorization(
        grant_id=_nonblank(getattr(value, "grant_id", None), "grant_id"),
        experiment_id=_nonblank(getattr(value, "experiment_id", None), "experiment_id"),
        specification_fingerprint=_digest(
            getattr(value, "specification_sha256", None), "specification_sha256"
        ),
        split_fingerprint=_digest(getattr(value, "split_fingerprint", None), "split_fingerprint"),
        candidate_configuration_hashes=candidates,
        issued_at=_utc_datetime(getattr(value, "issued_at", None), "issued_at"),
        consumed_at=(None if consumed_at is None else _utc_datetime(consumed_at, "consumed_at")),
    )


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise CompatibilityError(f"{name} must be bool")
    return value


def _fingerprint(value: object, name: str) -> str:
    fingerprint = getattr(value, "fingerprint", None)
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise CompatibilityError(f"{name} has no SHA-256 fingerprint")
    if any(character not in "0123456789abcdef" for character in fingerprint):
        raise CompatibilityError(f"{name} fingerprint is invalid")
    return fingerprint


def _research_parameter_dimension(
    research: Any,
    name: str,
    values: tuple[Any, ...],
) -> Any:
    kind = _parameter_kind(research, values)
    normalized = values
    if kind is research.ParameterKind.DECIMAL:
        normalized = tuple(
            value if isinstance(value, Decimal) else Decimal(str(value)) for value in values
        )
    return research.ParameterDimension(name=name, kind=kind, values=normalized)


def _parameter_kind(research: Any, values: tuple[Any, ...]) -> Any:
    if all(isinstance(value, bool) for value in values):
        return research.ParameterKind.BOOLEAN
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return research.ParameterKind.INTEGER
    if all(
        isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in values
    ):
        return research.ParameterKind.DECIMAL
    return research.ParameterKind.CATEGORICAL


def _walk_forward_policy(
    research: Any,
    request: StrategyFactoryRequest,
    split: ResearchSplitPlan,
) -> Any:
    raw = request.objectives.get("walk_forward", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise CompatibilityError("objectives.walk_forward must be a mapping")
    allowed = {
        "mode",
        "train_seconds",
        "validation_seconds",
        "oos_seconds",
        "step_seconds",
        "purge_seconds",
        "embargo_seconds",
        "minimum_history_seconds",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise CompatibilityError("unknown walk-forward settings: " + ", ".join(sorted(unknown)))
    total_seconds = int((split.test.end - split.train.start).total_seconds())
    if total_seconds < 10:
        raise CompatibilityError("pre-holdout period is too short for walk-forward")

    def positive(name: str, default: int) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CompatibilityError(f"walk-forward {name} must be a positive int")
        return int(value)

    def nonnegative(name: str, default: int) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CompatibilityError(f"walk-forward {name} must be a non-negative int")
        return int(value)

    train_seconds = positive("train_seconds", max(1, total_seconds // 2))
    validation_seconds = positive("validation_seconds", max(1, total_seconds // 6))
    oos_seconds = positive("oos_seconds", max(1, total_seconds // 10))
    step_seconds = positive("step_seconds", oos_seconds)
    mode_raw = raw.get("mode", "rolling")
    if not isinstance(mode_raw, str):
        raise CompatibilityError("walk-forward mode must be a string")
    try:
        mode = research.WalkForwardMode(mode_raw)
    except ValueError as exc:
        raise CompatibilityError("unsupported walk-forward mode") from exc
    return research.WalkForwardPolicy(
        mode=mode,
        train_seconds=train_seconds,
        validation_seconds=validation_seconds,
        oos_seconds=oos_seconds,
        step_seconds=step_seconds,
        purge_seconds=nonnegative("purge_seconds", 0),
        embargo_seconds=nonnegative("embargo_seconds", 0),
        minimum_history_seconds=nonnegative("minimum_history_seconds", train_seconds),
    )


def _research_constraint(research: Any, value: Mapping[str, Any]) -> Any:
    try:
        return research.ParameterConstraint(
            left=value["left"],
            operator=value["operator"],
            right_parameter=value.get("right_parameter"),
            right_value=value.get("right_value"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CompatibilityError("invalid Research Runner parameter constraint") from exc


def _file_size(uri: str) -> int:
    path = Path(uri[7:] if uri.startswith("file://") else uri)
    try:
        return path.stat().st_size
    except OSError as exc:
        raise CompatibilityError(f"cannot stat dataset: {path}") from exc


def _derive_score_components(metrics: Mapping[str, Any]) -> dict[str, Any]:
    desired = (
        "net_return",
        "profit_factor",
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
        "trade_count",
        "expectancy",
        "walk_forward_efficiency",
    )
    output: dict[str, Any] = {}
    for metric in desired:
        matches = [value for key, value in metrics.items() if key.endswith(f".{metric}")]
        if matches:
            output[metric] = matches[-1]
    output["complexity_penalty"] = 0
    return output
