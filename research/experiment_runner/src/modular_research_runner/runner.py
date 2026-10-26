from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .access import FinalTestAccessError, FinalTestGrant
from .artifacts import ArtifactReference, AtomicJsonArtifactStore
from .canonical import canonical_value
from .engine import BacktestJob
from .identity import ExperimentIdentityInputs
from .manifest import (
    EnvironmentManifest,
    ExperimentManifest,
    ExperimentManifestBuilder,
    GitProvenance,
)
from .models import ExperimentStatus, ResearchSpecification, SpecificationError
from .planning import ExperimentPlan, ResearchPlanBuilder, StageRole
from .policies import ResumePolicy, RetryPolicy
from .registry_package import RegistryPackageReference, RegistryPackageSink
from .repository import SqliteExperimentRepository, StateRepositoryError
from .results import ResultSink, RunResultEnvelope
from .scheduler import CancellationToken, LocalResearchScheduler, SchedulerSummary
from .state import RunStatus


@dataclass(frozen=True, slots=True)
class PreparedExperiment:
    specification: ResearchSpecification
    manifest: ExperimentManifest
    plan: ExperimentPlan
    specification_artifact: ArtifactReference
    manifest_artifact: ArtifactReference
    plan_artifact: ArtifactReference


class ResearchRunner:
    """High-level orchestrator for deterministic Modular Strategy research.

    It coordinates planning, persistence, official Backtest Engine execution,
    final-test access control, and result envelopes. It never implements strategy
    or financial metric calculations.
    """

    def __init__(
        self,
        *,
        repository: SqliteExperimentRepository,
        artifact_store: AtomicJsonArtifactStore,
        plan_builder: ResearchPlanBuilder,
        scheduler: LocalResearchScheduler,
        backtest_engine_version: str,
        metric_schema_version: str,
        module_api_version: str,
    ) -> None:
        for name, value in (
            ("backtest_engine_version", backtest_engine_version),
            ("metric_schema_version", metric_schema_version),
            ("module_api_version", module_api_version),
        ):
            if not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        self.repository = repository
        self.artifact_store = artifact_store
        self.plan_builder = plan_builder
        self.scheduler = scheduler
        self.backtest_engine_version = backtest_engine_version
        self.metric_schema_version = metric_schema_version
        self.module_api_version = module_api_version

    def prepare(
        self,
        specification: ResearchSpecification,
        *,
        git: GitProvenance,
        environment: EnvironmentManifest | None = None,
        parent_experiment_id: str | None = None,
        created_at: datetime | None = None,
    ) -> PreparedExperiment:
        timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
        worker_limit = specification.resource_limits.get(
            "maximum_workers", specification.parallelism
        )
        if not isinstance(worker_limit, int) or isinstance(worker_limit, bool) or worker_limit < 1:
            raise SpecificationError("resource_limits.maximum_workers must be positive")
        allowed_workers = min(worker_limit, specification.parallelism)
        if self.scheduler.maximum_workers > allowed_workers:
            raise SpecificationError(
                "scheduler maximum_workers exceeds the research specification limit"
            )
        expected_retry = RetryPolicy.from_mapping(specification.retry_policy)
        if self.scheduler.retry_policy != expected_retry:
            raise SpecificationError(
                "scheduler retry_policy differs from the frozen research specification"
            )
        expected_resume = ResumePolicy.from_mapping(specification.resume_policy)
        if self.scheduler.resume_policy != expected_resume:
            raise SpecificationError(
                "scheduler resume_policy differs from the frozen research specification"
            )
        experiment_id = ExperimentIdentityInputs(
            specification=specification,
            backtest_engine_version=self.backtest_engine_version,
            metric_schema_version=self.metric_schema_version,
            module_api_version=self.module_api_version,
        ).experiment_id()
        plan = self.plan_builder.build(
            experiment_id=experiment_id,
            specification=specification,
        )
        manifest = ExperimentManifestBuilder().build(
            experiment_id=experiment_id,
            specification=specification,
            configurations=plan.configurations,
            split=plan.split,
            git=git,
            backtest_engine_version=self.backtest_engine_version,
            metric_schema_version=self.metric_schema_version,
            module_api_version=self.module_api_version,
            scheduler_configuration={
                "type": "local",
                "maximum_workers": self.scheduler.maximum_workers,
                "executor_mode": self.scheduler.executor_mode,
                "retry_policy": self.scheduler.retry_policy,
                "resume_policy": self.scheduler.resume_policy,
            },
            environment=environment,
            parent_experiment_id=parent_experiment_id,
            created_at=timestamp,
        )
        manifest_mapping = canonical_value(manifest)
        if not isinstance(manifest_mapping, dict):
            raise SpecificationError("experiment manifest must canonicalize to an object")
        self.repository.register_experiment(
            experiment_id=experiment_id,
            specification_sha256=specification.fingerprint,
            manifest=manifest_mapping,
            created_at=timestamp,
        )
        self.repository.register_jobs(
            plan.jobs,
            max_attempts=self.scheduler.retry_policy.max_attempts,
            created_at=timestamp,
        )
        self.repository.set_experiment_status(
            experiment_id,
            ExperimentStatus.PLANNED,
            at=timestamp,
        )
        root = f"experiments/{experiment_id}"
        specification_artifact = self.artifact_store.write_json(
            f"{root}/specification.json", specification
        )
        manifest_artifact = self.artifact_store.write_json(f"{root}/manifest.json", manifest)
        plan_artifact = self.artifact_store.write_json(f"{root}/plan.json", plan)
        return PreparedExperiment(
            specification=specification,
            manifest=manifest,
            plan=plan,
            specification_artifact=specification_artifact,
            manifest_artifact=manifest_artifact,
            plan_artifact=plan_artifact,
        )

    def execute(
        self,
        experiment_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> SchedulerSummary:
        return self.scheduler.run(experiment_id, cancellation=cancellation)

    def activate_stage(
        self,
        prepared: PreparedExperiment,
        *,
        role: StageRole,
        candidate_configuration_hashes: Sequence[str],
        activated_at: datetime | None = None,
    ) -> tuple[str, ...]:
        if role not in {StageRole.VALIDATION, StageRole.TEST, StageRole.OOS}:
            raise SpecificationError("only validation, test, or OOS stages can be activated")
        status = self.repository.experiment_status(prepared.manifest.experiment_id)
        if status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.COMPLETED_WITH_FAILURES,
        }:
            raise SpecificationError("the preceding research stage has not finished")
        requested = tuple(sorted(set(candidate_configuration_hashes)))
        eligible = {item.sha256 for item in prepared.plan.configurations}
        if not requested or not set(requested) <= eligible:
            raise SpecificationError("stage candidates are empty or outside the frozen plan")
        prerequisite: StageRole | str
        if role is StageRole.VALIDATION and prepared.plan.optimization_stages:
            prerequisite = prepared.plan.optimization_stages[-1].role
        else:
            prerequisite = StageRole.TRAIN if role is StageRole.VALIDATION else StageRole.VALIDATION
        self._require_successful_candidates(
            prepared.manifest.experiment_id,
            role=prerequisite,
            candidate_configuration_hashes=requested,
        )
        jobs = self.plan_builder.build_stage_jobs(
            plan=prepared.plan,
            specification=prepared.specification,
            role=role,
            candidate_configuration_hashes=requested,
        )
        timestamp = activated_at or datetime.now(UTC)
        inserted = self.repository.activate_stage(
            experiment_id=prepared.manifest.experiment_id,
            target_role=role.value,
            candidate_configuration_hashes=requested,
            jobs=jobs,
            max_attempts=self.scheduler.retry_policy.max_attempts,
            activated_at=timestamp,
        )
        if inserted:
            self.repository.set_experiment_status(
                prepared.manifest.experiment_id,
                ExperimentStatus.PLANNED,
                at=timestamp,
            )
        self.artifact_store.write_json(
            f"experiments/{prepared.manifest.experiment_id}/promotions/{role.value}.json",
            {
                "target_role": role,
                "candidate_configuration_hashes": requested,
                "run_ids": tuple(job.run_id for job in jobs),
                "activated_at": timestamp,
            },
        )
        return tuple(job.run_id for job in jobs)

    def activate_optimization_stage(
        self,
        prepared: PreparedExperiment,
        *,
        stage_index: int,
        candidate_configuration_hashes: Sequence[str],
        activated_at: datetime | None = None,
    ) -> tuple[str, ...]:
        if not prepared.plan.optimization_stages:
            raise SpecificationError("experiment is not configured for staged search")
        if not isinstance(stage_index, int) or isinstance(stage_index, bool):
            raise SpecificationError("optimization stage_index must be an integer")
        available_indices = {stage.stage_index for stage in prepared.plan.optimization_stages}
        if stage_index not in available_indices or stage_index == 0:
            raise SpecificationError("optimization stage_index is not activatable")
        status = self.repository.experiment_status(prepared.manifest.experiment_id)
        if status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.COMPLETED_WITH_FAILURES,
        }:
            raise SpecificationError("the preceding optimization stage has not finished")
        requested = tuple(sorted(set(candidate_configuration_hashes)))
        eligible = {item.sha256 for item in prepared.plan.configurations}
        if not requested or not set(requested) <= eligible:
            raise SpecificationError("optimization candidates are empty or outside the frozen plan")
        previous_role = f"optimization_{stage_index - 1:03d}"
        self._require_successful_candidates(
            prepared.manifest.experiment_id,
            role=previous_role,
            candidate_configuration_hashes=requested,
        )
        jobs = self.plan_builder.build_optimization_stage_jobs(
            plan=prepared.plan,
            specification=prepared.specification,
            stage_index=stage_index,
            candidate_configuration_hashes=requested,
        )
        target_role = f"optimization_{stage_index:03d}"
        timestamp = activated_at or datetime.now(UTC)
        inserted = self.repository.activate_stage(
            experiment_id=prepared.manifest.experiment_id,
            target_role=target_role,
            candidate_configuration_hashes=requested,
            jobs=jobs,
            max_attempts=self.scheduler.retry_policy.max_attempts,
            activated_at=timestamp,
        )
        if inserted:
            self.repository.set_experiment_status(
                prepared.manifest.experiment_id,
                ExperimentStatus.PLANNED,
                at=timestamp,
            )
        self.artifact_store.write_json(
            f"experiments/{prepared.manifest.experiment_id}/promotions/{target_role}.json",
            {
                "target_role": target_role,
                "stage_index": stage_index,
                "source_role": previous_role,
                "candidate_configuration_hashes": requested,
                "run_ids": tuple(job.run_id for job in jobs),
                "activated_at": timestamp,
            },
        )
        return tuple(job.run_id for job in jobs)

    def _require_successful_candidates(
        self,
        experiment_id: str,
        *,
        role: StageRole | str,
        candidate_configuration_hashes: Sequence[str],
    ) -> None:
        role_value = role.value if isinstance(role, StageRole) else role
        if not role_value.strip():
            raise SpecificationError("candidate prerequisite role must be non-empty")
        requested = set(candidate_configuration_hashes)
        observed: dict[str, list[RunStatus]] = {item: [] for item in requested}
        for state in self.repository.list_runs(experiment_id):
            job = self.repository.get_job(state.run_id)
            candidate = job.metadata.get("configuration_sha256")
            if job.stage_role != role_value:
                continue
            if not isinstance(candidate, str):
                raise SpecificationError("candidate job is missing its configuration fingerprint")
            if candidate in requested:
                observed[candidate].append(state.status)
        missing = sorted(candidate for candidate, statuses in observed.items() if not statuses)
        unsuccessful = sorted(
            candidate
            for candidate, statuses in observed.items()
            if statuses and any(status is not RunStatus.SUCCEEDED for status in statuses)
        )
        if missing or unsuccessful:
            raise SpecificationError(
                f"candidate prerequisite is incomplete; missing={missing}, "
                f"unsuccessful={unsuccessful}"
            )

    def authorize_final_test(
        self,
        prepared: PreparedExperiment,
        *,
        candidate_configuration_hashes: Sequence[str],
        issued_at: datetime | None = None,
    ) -> FinalTestGrant:
        if not prepared.plan.final_stages:
            raise FinalTestAccessError("experiment has no final untouched test range")
        status = self.repository.experiment_status(prepared.manifest.experiment_id)
        if status not in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.COMPLETED_WITH_FAILURES,
        }:
            raise FinalTestAccessError(
                "ordinary research must finish before final test authorization"
            )
        eligible = {item.sha256 for item in prepared.plan.configurations}
        requested = set(candidate_configuration_hashes)
        if not requested or not requested <= eligible:
            raise FinalTestAccessError("final test candidates are empty or not in the frozen plan")
        preceding_role = (
            StageRole.OOS
            if any(stage.role is StageRole.OOS for stage in prepared.plan.stages)
            else StageRole.TEST
        )
        activated = self.repository.stage_activation_candidates(
            prepared.manifest.experiment_id, preceding_role.value
        )
        if activated is None or not requested <= set(activated):
            raise FinalTestAccessError(
                "final candidates were not frozen before the preceding evaluation stage"
            )
        try:
            self._require_successful_candidates(
                prepared.manifest.experiment_id,
                role=preceding_role,
                candidate_configuration_hashes=tuple(requested),
            )
        except SpecificationError as exc:
            raise FinalTestAccessError(
                "final candidates did not complete the preceding evaluation stage"
            ) from exc
        grant = FinalTestGrant.issue(
            experiment_id=prepared.manifest.experiment_id,
            specification_sha256=prepared.specification.fingerprint,
            split_fingerprint=prepared.plan.split.fingerprint,
            candidate_configuration_hashes=tuple(requested),
            issued_at=issued_at or datetime.now(UTC),
        )
        stored = self.repository.issue_final_test_grant(grant)
        self.artifact_store.write_json(
            f"experiments/{grant.experiment_id}/final-test/{grant.grant_id}.json",
            stored,
        )
        return stored

    def activate_final_test(
        self,
        prepared: PreparedExperiment,
        *,
        grant_id: str,
        activated_at: datetime | None = None,
    ) -> tuple[str, ...]:
        timestamp = activated_at or datetime.now(UTC)
        grant = self.repository.get_final_test_grant(grant_id)
        if grant.experiment_id != prepared.manifest.experiment_id:
            raise FinalTestAccessError("grant belongs to another experiment")
        if grant.specification_sha256 != prepared.specification.fingerprint:
            raise FinalTestAccessError("grant specification fingerprint has changed")
        if grant.split_fingerprint != prepared.plan.split.fingerprint:
            raise FinalTestAccessError("grant split fingerprint has changed")
        jobs = self.plan_builder.build_final_jobs(
            plan=prepared.plan,
            specification=prepared.specification,
            candidate_configuration_hashes=grant.candidate_configuration_hashes,
        )
        consumed, _ = self.repository.activate_final_test_grant(
            grant_id,
            jobs=jobs,
            max_attempts=self.scheduler.retry_policy.max_attempts,
            activated_at=timestamp,
        )
        self.repository.set_experiment_status(
            consumed.experiment_id,
            ExperimentStatus.PLANNED,
            at=timestamp,
        )
        self.artifact_store.write_json(
            f"experiments/{consumed.experiment_id}/final-test/{grant_id}-activation.json",
            {
                "grant": consumed,
                "run_ids": tuple(job.run_id for job in jobs),
                "activated_at": timestamp,
            },
        )
        return tuple(job.run_id for job in jobs)

    def _successful_result_envelopes(
        self,
        prepared: PreparedExperiment,
        *,
        metrics_artifacts: Mapping[str, ArtifactReference | None] | None = None,
        created_at: datetime | None = None,
    ) -> tuple[tuple[BacktestJob, RunResultEnvelope], ...]:
        metrics = metrics_artifacts or {}
        datasets = {item.dataset_id: item for item in prepared.specification.datasets}
        timestamp = created_at or datetime.now(UTC)
        envelopes: list[tuple[BacktestJob, RunResultEnvelope]] = []
        for state in self.repository.list_runs(
            prepared.manifest.experiment_id, statuses=(RunStatus.SUCCEEDED,)
        ):
            if state.result is None:
                raise StateRepositoryError("successful run is missing its result reference")
            job = self.repository.get_job(state.run_id)
            configuration_sha256 = job.metadata.get("configuration_sha256")
            if not isinstance(configuration_sha256, str):
                raise SpecificationError("job is missing its configuration fingerprint")
            dataset = datasets[job.dataset_id]
            envelopes.append(
                (
                    job,
                    RunResultEnvelope(
                        experiment_id=job.experiment_id,
                        run_id=job.run_id,
                        configuration_sha256=configuration_sha256,
                        stage_id=job.stage_id,
                        stage_role=job.stage_role,
                        split_id=job.split_id,
                        dataset_id=job.dataset_id,
                        dataset_sha256=dataset.sha256,
                        scenario_id=job.scenario_id,
                        seed=job.seed,
                        engine=state.result,
                        metrics_schema_version=self.metric_schema_version,
                        metrics_artifact=metrics.get(job.run_id),
                        metadata={
                            "scenario_category": job.metadata.get("scenario_category"),
                            "root_seed": job.metadata.get("root_seed"),
                            "repetition_index": job.repetition_index,
                        },
                        created_at=timestamp,
                        schema_version=(
                            2 if state.result.effective_config_artifact is not None else 1
                        ),
                    ),
                )
            )
        return tuple(envelopes)

    def publish_result_envelopes(
        self,
        prepared: PreparedExperiment,
        *,
        sink: ResultSink,
        metrics_artifacts: Mapping[str, ArtifactReference | None] | None = None,
        created_at: datetime | None = None,
    ) -> tuple[ArtifactReference, ...]:
        return tuple(
            sink.publish(envelope)
            for _, envelope in self._successful_result_envelopes(
                prepared,
                metrics_artifacts=metrics_artifacts,
                created_at=created_at,
            )
        )

    def publish_registry_packages(
        self,
        prepared: PreparedExperiment,
        *,
        sink: RegistryPackageSink,
        metrics_artifacts: Mapping[str, ArtifactReference | None] | None = None,
        created_at: datetime | None = None,
    ) -> tuple[RegistryPackageReference, ...]:
        references: list[RegistryPackageReference] = []
        for job, envelope in self._successful_result_envelopes(
            prepared,
            metrics_artifacts=metrics_artifacts,
            created_at=created_at,
        ):
            references.append(
                sink.publish(
                    envelope=envelope,
                    experiment_manifest=prepared.manifest,
                    specification=prepared.specification,
                    job=job,
                )
            )
        return tuple(references)
