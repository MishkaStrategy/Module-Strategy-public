"""Canonical publication from Research Runner packages into Results Aggregator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .adapters import ResearchFrozenConfiguration, ResearchRunnerExecutionAdapter
from .canonical import canonical_sha256
from .contracts import DatasetGateStatus, DatasetValidationReport, StrategyFactoryRequest
from .errors import CompatibilityError, ExternalComponentUnavailableError

_REQUIRED_RESEARCH_PACKAGE_FILES = frozenset(
    {
        "run.json",
        "metrics.json",
        "journal.json",
        "trades.csv",
        "equity.csv",
        "events.jsonl",
        "application-run.json",
        "metrics-extended.json",
        "data-validation.json",
        "restart-evidence.json",
        "artifact-manifest.json",
    }
)
_SUCCESS_STATUSES = frozenset({"completed", "completed_with_failures"})


@dataclass(frozen=True, slots=True)
class ResultsPublicationPolicy:
    """Explicit, stable metadata required by the Results Aggregator intake."""

    worker_id: str = "strategy-factory"
    machine_id: str = "local-research"
    preprocessing_version: str = "strategy-factory-v1"
    aggregation_version: str = "source-declared-v1"
    execution_assumptions: Mapping[str, Any] = field(default_factory=dict)
    risk_settings: Mapping[str, Any] = field(default_factory=dict)
    cost_settings: Mapping[str, Any] = field(default_factory=dict)
    funding_settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "worker_id",
            "machine_id",
            "preprocessing_version",
            "aggregation_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CompatibilityError(f"{name} must be a non-blank string")
        for name in (
            "execution_assumptions",
            "risk_settings",
            "cost_settings",
            "funding_settings",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise CompatibilityError(f"{name} must be a mapping")
            object.__setattr__(
                self,
                name,
                MappingProxyType(dict(value)),
            )


@dataclass(frozen=True, slots=True)
class PublishedRegistryRun:
    """Verified identity returned after one atomic Results Aggregator registration."""

    research_package_id: str
    registration_package_id: str
    experiment_id: str
    run_id: str
    configuration_id: str
    native_configuration_hash: str
    factory_configuration_hash: str
    validation_hash: str
    registry_state: str
    registry_revision: int
    registered_entities: int
    duplicate_entities: int

    def __post_init__(self) -> None:
        for name in (
            "research_package_id",
            "registration_package_id",
            "experiment_id",
            "run_id",
            "configuration_id",
            "registry_state",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CompatibilityError(f"{name} must be a non-blank string")
        for name in (
            "native_configuration_hash",
            "factory_configuration_hash",
            "validation_hash",
        ):
            _digest(getattr(self, name), name)
        for name in (
            "registry_revision",
            "registered_entities",
            "duplicate_entities",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CompatibilityError(f"{name} must be a non-negative int")
        if self.registry_state not in {"REGISTERED", "DUPLICATE"}:
            raise CompatibilityError("registry_state must be REGISTERED or DUPLICATE")


class ResearchResultsPublicationAdapter:
    """Publish, validate, adapt, and atomically register successful research runs.

    Research Runner remains the canonical producer of immutable artifact packages.
    Results Aggregator remains the canonical registry and metric source. This adapter
    only bridges their released contracts and preserves both Research Runner and Core
    configuration identities.
    """

    def __init__(
        self,
        *,
        execution: ResearchRunnerExecutionAdapter,
        request: StrategyFactoryRequest,
        package_root: str | Path,
        registry_path: str | Path,
        producer_module_version: str,
        policy: ResultsPublicationPolicy | None = None,
        dataset_report: DatasetValidationReport | None = None,
    ) -> None:
        if not isinstance(execution, ResearchRunnerExecutionAdapter):
            raise TypeError("execution must be ResearchRunnerExecutionAdapter")
        if not isinstance(request, StrategyFactoryRequest):
            raise TypeError("request must be StrategyFactoryRequest")
        if not isinstance(producer_module_version, str) or not producer_module_version.strip():
            raise CompatibilityError("producer_module_version must be non-blank")
        if dataset_report is not None:
            if not isinstance(dataset_report, DatasetValidationReport):
                raise TypeError("dataset_report must be DatasetValidationReport or None")
            if dataset_report.dataset_id != request.dataset.dataset_id:
                raise CompatibilityError("dataset report belongs to another dataset")
            if dataset_report.dataset_fingerprint != request.dataset.fingerprint:
                raise CompatibilityError("dataset report fingerprint does not match request")
            if dataset_report.computed_sha256 != request.dataset.sha256:
                raise CompatibilityError("dataset report hash does not match request")
            if dataset_report.status is DatasetGateStatus.REJECTED:
                raise CompatibilityError("rejected dataset cannot enter Results Aggregator")
        try:
            from modular_research_runner import LocalResultsAggregatorPackageSink
            from modular_results_registry import SQLiteRegistry
        except ImportError as exc:
            raise ExternalComponentUnavailableError(
                "Research Runner or Results Aggregator publication API is unavailable"
            ) from exc
        self._execution = execution
        self._request = request
        self._policy = policy or ResultsPublicationPolicy()
        self._dataset_report = dataset_report
        self._sink = LocalResultsAggregatorPackageSink(
            Path(package_root),
            producer_module_version=producer_module_version,
        )
        self._registry = SQLiteRegistry(registry_path)

    def publish_experiment(
        self,
        experiment_id: str,
        *,
        observed_at: datetime,
        baseline_native_hashes: frozenset[str] = frozenset(),
        finalist_native_hashes: frozenset[str] = frozenset(),
    ) -> tuple[PublishedRegistryRun, ...]:
        """Register every successful run exactly once after a terminal experiment."""

        experiment_id = _text(experiment_id, "experiment_id")
        observed_at = _utc(observed_at, "observed_at")
        for digest in (*baseline_native_hashes, *finalist_native_hashes):
            _digest(digest, "candidate native configuration hash")
        if baseline_native_hashes & finalist_native_hashes:
            raise CompatibilityError("a configuration cannot be both baseline and finalist")
        snapshot = self._execution.status(experiment_id)
        if snapshot.has_nonterminal_runs or snapshot.status not in _SUCCESS_STATUSES:
            raise CompatibilityError(
                "Results publication requires a terminal successful Research Runner experiment"
            )
        lineage = self._execution.publication_lineage(experiment_id)
        dependency_lock = lineage.get("dependency_lock_hash")
        if not isinstance(dependency_lock, str):
            raise CompatibilityError(
                "Results publication requires a frozen dependency lock SHA-256"
            )
        _digest(dependency_lock, "dependency_lock_hash")
        references = self._execution.publish_registry_packages(
            experiment_id,
            sink=self._sink,
            created_at=observed_at,
        )
        if not references:
            raise CompatibilityError(
                "terminal Research Runner experiment has no successful packages"
            )
        outcomes = tuple(
            self._register_reference(
                reference,
                lineage=lineage,
                experiment_status=snapshot.status,
                baseline_native_hashes=baseline_native_hashes,
                finalist_native_hashes=finalist_native_hashes,
            )
            for reference in sorted(
                references, key=lambda item: str(getattr(item, "package_id", ""))
            )
        )
        run_ids = tuple(item.run_id for item in outcomes)
        if len(run_ids) != len(set(run_ids)):
            raise CompatibilityError(
                "Research Runner publication returned duplicate run identities"
            )
        return outcomes

    def _register_reference(
        self,
        reference: Any,
        *,
        lineage: Mapping[str, Any],
        experiment_status: str,
        baseline_native_hashes: frozenset[str],
        finalist_native_hashes: frozenset[str],
    ) -> PublishedRegistryRun:
        try:
            from modular_results_registry import (
                CandidateRole,
                ConfigurationRecord,
                DatasetRecord,
                ExperimentRecord,
                ExperimentStatus,
                RunStatus,
            )
            from modular_results_registry.core_adapter import (
                CoreRunContext,
                adapt_core_v04_export,
            )
            from modular_results_registry.intake import validate_artifact_package
            from modular_results_registry.intake_models import ProducerProfile
        except ImportError as exc:
            raise ExternalComponentUnavailableError(
                "Results Aggregator intake or Core adapter API is unavailable"
            ) from exc

        package_directory = Path(getattr(reference, "package_directory", Path("")))
        package_id = _text(getattr(reference, "package_id", None), "package_id")
        if package_directory.is_symlink() or not package_directory.is_dir():
            raise CompatibilityError("published research package is not a real directory")
        profile = ProducerProfile(
            producer_module_id="modular-research-runner",
            allowed_producer_schema_versions=frozenset({"1"}),
            allowed_artifact_schema_prefixes=("modular-core.", "modular-backtest."),
            required_artifact_paths=_REQUIRED_RESEARCH_PACKAGE_FILES,
        )
        verified = validate_artifact_package(package_directory, profile)
        manifest = verified.manifest
        if manifest.package_id != package_id:
            raise CompatibilityError("Research Runner package reference identity changed")
        if manifest.experiment_id != lineage["experiment_id"]:
            raise CompatibilityError("research package belongs to another experiment")
        if manifest.commit_sha != lineage["commit_sha"]:
            raise CompatibilityError("research package commit SHA changed")
        if manifest.dataset_ids != (self._request.dataset.dataset_id,):
            raise CompatibilityError("research package dataset ID changed")
        if manifest.dataset_hashes != (self._request.dataset.sha256,):
            raise CompatibilityError("research package dataset hash changed")
        configurations = lineage.get("configurations")
        if not isinstance(configurations, Mapping):
            raise CompatibilityError("Research Runner lineage configurations are invalid")
        frozen = configurations.get(manifest.configuration_hash)
        if not isinstance(frozen, ResearchFrozenConfiguration):
            raise CompatibilityError("research package configuration is outside the frozen plan")
        if frozen.native_configuration_hash != manifest.configuration_hash:
            raise CompatibilityError("native configuration identity changed")
        core_metadata = _core_metadata(package_directory)
        module_versions = dict(lineage["module_versions"])
        for name, version in core_metadata["module_versions"].items():
            existing = module_versions.get(name)
            if existing is not None and existing != version:
                raise CompatibilityError(
                    f"module version conflict for {name}: {existing} != {version}"
                )
            module_versions[name] = version

        dataset = self._dataset_record(DatasetRecord)
        configuration = ConfigurationRecord.create(
            strategy_id=_text(lineage["strategy_id"], "strategy_id"),
            schema_version="1",
            canonical_configuration=dict(frozen.parameters),
            stage="research_runner_frozen",
            execution_assumptions=self._policy.execution_assumptions,
            risk_settings=self._policy.risk_settings,
            cost_settings=self._policy.cost_settings,
            funding_settings=self._policy.funding_settings,
            creation_source="STRATEGY_FACTORY_RESEARCH_RUNNER",
            producer_configuration_hash=None,
        )
        completed_at = manifest.created_at
        status = (
            ExperimentStatus.COMPLETED
            if experiment_status == "completed"
            else ExperimentStatus.COMPLETED_WITH_FAILURES
        )
        experiment = ExperimentRecord(
            experiment_id=_text(lineage["experiment_id"], "experiment_id"),
            research_id=_text(lineage["research_id"], "research_id"),
            title=_text(lineage["title"], "title"),
            description=str(lineage["description"]),
            strategy_id=_text(lineage["strategy_id"], "strategy_id"),
            strategy_version=_text(lineage["strategy_version"], "strategy_version"),
            commit_sha=_text(lineage["commit_sha"], "commit_sha"),
            branch=_text(lineage["branch"], "branch"),
            working_tree_status=_text(lineage["working_tree_status"], "working_tree_status"),
            diff_hash=lineage["diff_hash"],
            patch_artifact_id=None,
            module_versions=module_versions,
            dependency_lock_hash=_digest(lineage["dependency_lock_hash"], "dependency_lock_hash"),
            started_at=_utc(lineage["started_at"], "started_at"),
            completed_at=completed_at,
            status=status,
            search_method=_text(lineage["search_method"], "search_method"),
            split_policy=_text(lineage["split_policy"], "split_policy"),
            parent_experiment_id=lineage["parent_experiment_id"],
            tags=("strategy-factory", "research-runner-package-v1"),
        )
        split_information = manifest.split_information
        stage_role = _text(split_information.get("stage_role"), "stage_role")
        split_id = _text(split_information.get("split_id"), "split_id")
        role = self._candidate_role(
            CandidateRole,
            native_hash=manifest.configuration_hash,
            stage_role=stage_role,
            baseline_native_hashes=baseline_native_hashes,
            finalist_native_hashes=finalist_native_hashes,
        )
        context = CoreRunContext(
            experiment=experiment,
            configuration=configuration,
            datasets=(dataset,),
            stage_id=manifest.stage_id,
            split_id=split_id,
            scenario_id=manifest.scenario_id,
            seed=manifest.seed,
            worker_id=self._policy.worker_id,
            machine_id=self._policy.machine_id,
            run_status=RunStatus.COMPLETED,
            observed_completed_at=completed_at,
            duration_seconds=None,
            role=role,
            role_reason=self._role_reason(role, stage_role),
            metric_split=stage_role,
            metric_period=(
                f"{split_information.get('period_start')}/{split_information.get('period_end')}"
            ),
            metric_scenario=manifest.scenario_id,
        )
        imported = adapt_core_v04_export(package_directory, context)
        if len(imported.package.configurations) != 1:
            raise CompatibilityError("Core adapter returned an invalid configuration set")
        imported_configuration = imported.package.configurations[0]
        if imported_configuration.configuration_id != configuration.configuration_id:
            raise CompatibilityError("Core adapter changed configuration identity")
        enriched_configuration = replace(
            imported_configuration,
            producer_configuration_hash=manifest.configuration_hash,
        )
        registration_package = replace(
            imported.package,
            configurations=(enriched_configuration,),
        )
        outcome = self._registry.register_package(registration_package)
        if outcome.package_id != registration_package.package_id:
            raise CompatibilityError("Results Aggregator returned another package ID")
        run = registration_package.runs[0]
        return PublishedRegistryRun(
            research_package_id=manifest.package_id,
            registration_package_id=registration_package.package_id,
            experiment_id=manifest.experiment_id,
            run_id=run.run_id,
            configuration_id=enriched_configuration.configuration_id,
            native_configuration_hash=manifest.configuration_hash,
            factory_configuration_hash=frozen.factory_configuration_hash,
            validation_hash=verified.validation_hash,
            registry_state=outcome.state,
            registry_revision=outcome.registry_revision,
            registered_entities=outcome.registered_entities,
            duplicate_entities=outcome.duplicate_entities,
        )

    def _dataset_record(self, record_type: Any) -> Any:
        descriptor = self._request.dataset
        report = self._dataset_report
        gap_summary: Mapping[str, Any]
        duplicate_summary: Mapping[str, Any]
        quality_status: str
        if report is None:
            gap_summary = MappingProxyType({})
            duplicate_summary = MappingProxyType({})
            quality_status = "FACTORY_DATA_GATE_ACCEPTED"
        else:
            gap_summary = MappingProxyType(
                {
                    "gaps": report.gaps,
                    "out_of_order": report.out_of_order,
                    "reason_codes": report.reason_codes,
                    "warnings": report.warnings,
                }
            )
            duplicate_summary = MappingProxyType({"duplicates": report.duplicates})
            quality_status = report.status.value
        metadata_hash = canonical_sha256(
            {
                "dataset_fingerprint": descriptor.fingerprint,
                "received_at": descriptor.received_at,
                "exchange_info_sha256": descriptor.exchange_info_sha256,
                "file_format": descriptor.file_format,
            }
        )
        return record_type(
            dataset_id=descriptor.dataset_id,
            source=descriptor.source,
            market=descriptor.exchange,
            market_type=descriptor.market_type.value,
            instrument=descriptor.symbol,
            timeframe=descriptor.base_timeframe,
            min_timestamp=descriptor.period.start,
            max_timestamp=descriptor.period.end,
            row_count=descriptor.row_count,
            sha256=descriptor.sha256,
            preprocessing_version=self._policy.preprocessing_version,
            aggregation_version=self._policy.aggregation_version,
            metadata_hash=metadata_hash,
            funding_data_hash=descriptor.funding_sha256,
            gap_summary=gap_summary,
            duplicate_summary=duplicate_summary,
            quality_status=quality_status,
            timezone_name=descriptor.timezone,
        )

    @staticmethod
    def _candidate_role(
        candidate_role: Any,
        *,
        native_hash: str,
        stage_role: str,
        baseline_native_hashes: frozenset[str],
        finalist_native_hashes: frozenset[str],
    ) -> Any:
        if native_hash in baseline_native_hashes:
            return candidate_role.BASELINE
        if native_hash in finalist_native_hashes or stage_role == "final_untouched":
            return candidate_role.FINALIST
        return candidate_role.CANDIDATE

    @staticmethod
    def _role_reason(role: Any, stage_role: str) -> str:
        value = getattr(role, "value", str(role))
        return f"STRATEGY_FACTORY_{value}_{stage_role.upper()}"

    def close(self) -> None:
        self._registry.close()


def _core_metadata(package_directory: Path) -> Mapping[str, Any]:
    path = package_directory / "run.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompatibilityError("verified Core run document cannot be decoded") from exc
    if not isinstance(document, Mapping):
        raise CompatibilityError("Core run document must be a mapping")
    manifest = document.get("manifest")
    if not isinstance(manifest, Mapping):
        raise CompatibilityError("Core run manifest is missing")
    kernel_version = _text(manifest.get("kernel_version"), "kernel_version")
    module_versions: dict[str, str] = {"core": kernel_version}
    raw_modules = manifest.get("module_manifests")
    if not isinstance(raw_modules, list):
        raise CompatibilityError("Core module manifests must be a list")
    for index, item in enumerate(raw_modules):
        if not isinstance(item, Mapping):
            raise CompatibilityError(f"Core module_manifests[{index}] must be a mapping")
        name = _text(item.get("name"), f"module_manifests[{index}].name")
        version = _text(item.get("version"), f"module_manifests[{index}].version")
        if name in module_versions:
            raise CompatibilityError(f"duplicate Core module manifest: {name}")
        module_versions[name] = version
    return MappingProxyType(
        {"kernel_version": kernel_version, "module_versions": MappingProxyType(module_versions)}
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompatibilityError(f"{name} must be a non-blank string")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CompatibilityError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompatibilityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
