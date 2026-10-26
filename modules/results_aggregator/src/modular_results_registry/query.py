from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Generic, Mapping, TypeVar

from .errors import IntegrityError
from .models import (
    ArtifactRecord,
    AuditEvidenceRecord,
    CandidateRole,
    ConfigurationRecord,
    DatasetRecord,
    ExperimentRecord,
    MetricDefinitionRecord,
    MetricSnapshot,
    RoleDecision,
    RunRecord,
)
from .serialization import (
    artifact_from_mapping,
    configuration_from_mapping,
    dataset_from_mapping,
    evidence_from_mapping,
    experiment_from_mapping,
    metric_definition_from_mapping,
    metric_from_mapping,
    role_from_mapping,
    run_from_mapping,
)
from .storage import SQLiteRegistry

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    offset: int
    limit: int
    next_offset: int | None

    @property
    def has_more(self) -> bool:
        return self.next_offset is not None


class RegistryQueryAPI:
    """Read-only typed query facade over immutable registry records."""

    def __init__(self, registry: SQLiteRegistry, *, timeout_seconds: float = 30.0) -> None:
        if not isinstance(registry, SQLiteRegistry):
            raise TypeError("registry must be a SQLiteRegistry")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self._registry = registry
        self._timeout_seconds = float(timeout_seconds)

    def artifact(self, artifact_id: str) -> ArtifactRecord | None:
        return self._one("artifact", artifact_id, artifact_from_mapping)

    def dataset(self, dataset_id: str) -> DatasetRecord | None:
        return self._one("dataset", dataset_id, dataset_from_mapping)

    def configuration(self, configuration_id: str) -> ConfigurationRecord | None:
        return self._one("configuration", configuration_id, configuration_from_mapping)

    def experiment(self, experiment_id: str) -> ExperimentRecord | None:
        return self._one("experiment", experiment_id, experiment_from_mapping)

    def run(self, run_id: str) -> RunRecord | None:
        return self._one("run", run_id, run_from_mapping)

    def metric_definition(self, definition_id: str) -> MetricDefinitionRecord | None:
        return self._one("metric_definition", definition_id, metric_definition_from_mapping)

    def evidence(self, evidence_id: str) -> AuditEvidenceRecord | None:
        return self._one("evidence", evidence_id, evidence_from_mapping)

    def experiments(
        self,
        *,
        strategy_id: str | None = None,
        research_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[ExperimentRecord]:
        filters = _drop_none(
            {"strategy_id": strategy_id, "research_id": research_id, "status": status}
        )
        return self._page("experiment", filters, experiment_from_mapping, limit, offset)

    def runs(
        self,
        *,
        experiment_id: str | None = None,
        configuration_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[RunRecord]:
        filters = _drop_none(
            {
                "experiment_id": experiment_id,
                "configuration_id": configuration_id,
                "status": status,
            }
        )
        return self._page("run", filters, run_from_mapping, limit, offset)

    def metric_definitions(
        self,
        *,
        metric_name: str | None = None,
        definition_version: str | None = None,
        owner_module: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Page[MetricDefinitionRecord]:
        filters = _drop_none(
            {
                "metric_name": metric_name,
                "definition_version": definition_version,
                "owner_module": owner_module,
            }
        )
        return self._page(
            "metric_definition",
            filters,
            metric_definition_from_mapping,
            limit,
            offset,
        )

    def metrics_for_run(
        self,
        run_id: str,
        *,
        metric_name: str | None = None,
        definition_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Page[MetricSnapshot]:
        filters = _drop_none(
            {
                "run_id": run_id,
                "metric_name": metric_name,
                "definition_id": definition_id,
            }
        )
        return self._page("metric", filters, metric_from_mapping, limit, offset)

    def evidence_for_experiment(
        self,
        experiment_id: str,
        *,
        evidence_type: str | None = None,
        status: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Page[AuditEvidenceRecord]:
        filters = _drop_none(
            {
                "experiment_id": experiment_id,
                "evidence_type": evidence_type,
                "status": status,
            }
        )
        return self._page("evidence", filters, evidence_from_mapping, limit, offset)

    def roles(
        self,
        experiment_id: str,
        *,
        role: CandidateRole | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Page[RoleDecision]:
        filters: dict[str, str] = {"experiment_id": experiment_id}
        if role is not None:
            if not isinstance(role, CandidateRole):
                raise TypeError("role must be a CandidateRole")
            filters["role"] = role.value
        return self._page("role", filters, role_from_mapping, limit, offset)

    def baselines(self, experiment_id: str) -> tuple[ConfigurationRecord, ...]:
        return self._configurations_for_role(experiment_id, CandidateRole.BASELINE)

    def candidates(self, experiment_id: str) -> tuple[ConfigurationRecord, ...]:
        accepted = {
            CandidateRole.CANDIDATE,
            CandidateRole.ABLATION,
            CandidateRole.CONTROL,
            CandidateRole.FINALIST,
            CandidateRole.REFERENCE,
        }
        decisions = self._all_roles(experiment_id)
        configuration_ids = sorted(
            {item.configuration_id for item in decisions if item.role in accepted}
        )
        return self._configurations(configuration_ids)

    def artifacts_for_run(self, run_id: str) -> tuple[ArtifactRecord, ...]:
        run = self.run(run_id)
        if run is None:
            return ()
        result: list[ArtifactRecord] = []
        for artifact_id in sorted(run.artifact_ids):
            artifact = self.artifact(artifact_id)
            if artifact is None:
                raise IntegrityError("run references a missing artifact")
            result.append(artifact)
        return tuple(result)

    def datasets_for_run(self, run_id: str) -> tuple[DatasetRecord, ...]:
        run = self.run(run_id)
        if run is None:
            return ()
        result: list[DatasetRecord] = []
        for dataset_id in sorted(run.dataset_ids):
            dataset = self.dataset(dataset_id)
            if dataset is None:
                raise IntegrityError("run references a missing dataset")
            result.append(dataset)
        return tuple(result)

    def _configurations_for_role(
        self, experiment_id: str, role: CandidateRole
    ) -> tuple[ConfigurationRecord, ...]:
        decisions = self._all_roles(experiment_id, role=role)
        return self._configurations(sorted({item.configuration_id for item in decisions}))

    def _all_roles(
        self, experiment_id: str, *, role: CandidateRole | None = None
    ) -> tuple[RoleDecision, ...]:
        items: list[RoleDecision] = []
        offset = 0
        while True:
            page = self.roles(
                experiment_id,
                role=role,
                limit=10_000,
                offset=offset,
            )
            items.extend(page.items)
            if page.next_offset is None:
                return tuple(items)
            if page.next_offset <= offset:
                raise IntegrityError("role pagination cursor did not advance")
            offset = page.next_offset

    def _configurations(self, ids: list[str]) -> tuple[ConfigurationRecord, ...]:
        result: list[ConfigurationRecord] = []
        for configuration_id in ids:
            configuration = self.configuration(configuration_id)
            if configuration is None:
                raise IntegrityError("role references a missing configuration")
            result.append(configuration)
        return tuple(result)

    def _one(
        self, entity_type: str, entity_id: str, parser: Callable[[Mapping[str, object]], T]
    ) -> T | None:
        with self._registry.read_deadline(self._timeout_seconds):
            payload = self._registry.get_payload(entity_type, entity_id)
        return None if payload is None else parser(payload)

    def _page(
        self,
        entity_type: str,
        filters: Mapping[str, str | None],
        parser: Callable[[Mapping[str, object]], T],
        limit: int,
        offset: int,
    ) -> Page[T]:
        if limit < 1 or limit > 10_000 or offset < 0:
            raise ValueError("invalid pagination")
        with self._registry.read_deadline(self._timeout_seconds):
            raw = self._registry.query_payloads(
                entity_type,
                filters=filters,
                limit=limit + 1,
                offset=offset,
            )
        items = tuple(parser(item) for item in raw[:limit])
        next_offset = offset + limit if len(raw) > limit else None
        return Page(items=items, offset=offset, limit=limit, next_offset=next_offset)


def _drop_none(values: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}
