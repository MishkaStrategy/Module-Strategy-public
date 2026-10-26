from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from .errors import IntegrityError
from .models import (
    ExperimentRecord,
    RegistrationPackage,
)
from .storage_common import (
    _ConflictDetails,
    _DetectedConflict,
    _sql_identifier,
    _sql_identifier_list,
)

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationSupportMixin(StorageMixinBase):
    @staticmethod
    def _existing_hash(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_id: str,
    ) -> str | None:
        query = (
            "SELECT content_hash FROM "
            + _sql_identifier(table)
            + " WHERE "
            + _sql_identifier(id_column)
            + "=?"
        )
        row = connection.execute(query, (entity_id,)).fetchone()
        return None if row is None else str(row[0])

    def _insert_immutable(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_type: str,
        entity_id: str,
        content_hash: str,
        payload: bytes,
        columns: Sequence[str],
        values: Sequence[Any],
        correlation_id: str,
    ) -> bool:
        existing = self._existing_hash(
            connection,
            table=table,
            id_column=id_column,
            entity_id=entity_id,
        )
        if existing is not None:
            if existing == content_hash:
                return False
            raise _DetectedConflict(
                _ConflictDetails(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    existing_content_hash=existing,
                    incoming_content_hash=content_hash,
                    incoming_payload=payload,
                    correlation_id=correlation_id,
                )
            )
        placeholders = ",".join("?" for _ in columns)
        query = (
            "INSERT INTO "
            + _sql_identifier(table)
            + "("
            + _sql_identifier_list(columns)
            + ") VALUES("
            + placeholders
            + ")"
        )
        connection.execute(query, tuple(values))
        return True

    @staticmethod
    def _contains(
        connection: sqlite3.Connection,
        table: str,
        id_column: str,
        entity_id: str,
    ) -> bool:
        query = (
            "SELECT 1 FROM "
            + _sql_identifier(table)
            + " WHERE "
            + _sql_identifier(id_column)
            + "=?"
        )
        return connection.execute(query, (entity_id,)).fetchone() is not None

    def _validate_references(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
    ) -> None:
        artifact_ids = {item.artifact_id for item in package.artifacts}
        dataset_ids = {item.dataset_id for item in package.datasets}
        config_ids = {item.configuration_id for item in package.configurations}
        experiment_ids = {item.experiment_id for item in package.experiments}
        run_ids = {item.run_id for item in package.runs}
        definitions_by_id = {item.definition_id: item for item in package.metric_definitions}

        def available(table: str, column: str, entity_id: str, incoming: set[str]) -> bool:
            return entity_id in incoming or self._contains(connection, table, column, entity_id)

        for experiment in package.experiments:
            if experiment.parent_experiment_id is not None and not available(
                "experiments", "experiment_id", experiment.parent_experiment_id, experiment_ids
            ):
                raise IntegrityError(
                    f"experiment {experiment.experiment_id} references missing parent"
                )
            if experiment.patch_artifact_id is not None and not available(
                "artifacts", "artifact_id", experiment.patch_artifact_id, artifact_ids
            ):
                raise IntegrityError(
                    f"experiment {experiment.experiment_id} references missing patch artifact"
                )

        for run in package.runs:
            if not available("experiments", "experiment_id", run.experiment_id, experiment_ids):
                raise IntegrityError(f"run {run.run_id} references missing experiment")
            if not available(
                "configurations", "configuration_id", run.configuration_id, config_ids
            ):
                raise IntegrityError(f"run {run.run_id} references missing configuration")
            missing_datasets = [
                item
                for item in run.dataset_ids
                if not available("datasets", "dataset_id", item, dataset_ids)
            ]
            if missing_datasets:
                raise IntegrityError(
                    f"run {run.run_id} references missing datasets: {missing_datasets}"
                )
            missing_artifacts = [
                item
                for item in run.artifact_ids
                if not available("artifacts", "artifact_id", item, artifact_ids)
            ]
            if missing_artifacts:
                raise IntegrityError(
                    f"run {run.run_id} references missing artifacts: {missing_artifacts}"
                )

        for metric in package.metrics:
            if not available("runs", "run_id", metric.run_id, run_ids):
                raise IntegrityError(
                    f"metric {metric.metric_key} references missing run {metric.run_id}"
                )
            definition = definitions_by_id.get(metric.definition_id)
            if definition is None:
                row = connection.execute(
                    "SELECT payload_json, content_hash FROM metric_definitions "
                    "WHERE definition_id=?",
                    (metric.definition_id,),
                ).fetchone()
                if row is None:
                    raise IntegrityError(
                        f"metric {metric.metric_key} references missing metric definition "
                        f"{metric.definition_id}"
                    )
                stored = self._decode_stored_payload(
                    "metric_definition", row["payload_json"], row["content_hash"]
                )
                definition_unit = stored.get("unit")
                definition_precision = stored.get("precision")
                definition_producer = stored.get("producer")
            else:
                definition_unit = definition.unit
                definition_precision = definition.precision
                definition_producer = definition.producer
            if metric.unit != definition_unit:
                raise IntegrityError(
                    f"metric {metric.metric_key} unit does not match its definition"
                )
            if metric.precision != definition_precision:
                raise IntegrityError(
                    f"metric {metric.metric_key} precision does not match its definition"
                )
            if metric.producer != definition_producer:
                raise IntegrityError(
                    f"metric {metric.metric_key} producer does not match its definition"
                )

        for evidence in package.evidence:
            if not available(
                "experiments", "experiment_id", evidence.experiment_id, experiment_ids
            ):
                raise IntegrityError(
                    f"evidence {evidence.evidence_id} references missing experiment"
                )
            missing_runs = [
                item for item in evidence.run_ids if not available("runs", "run_id", item, run_ids)
            ]
            if missing_runs:
                raise IntegrityError(
                    f"evidence {evidence.evidence_id} references missing runs: {missing_runs}"
                )
            missing_artifacts = [
                item
                for item in evidence.artifact_ids
                if not available("artifacts", "artifact_id", item, artifact_ids)
            ]
            if missing_artifacts:
                raise IntegrityError(
                    f"evidence {evidence.evidence_id} references missing artifacts: {missing_artifacts}"
                )

        for role in package.roles:
            if not available("experiments", "experiment_id", role.experiment_id, experiment_ids):
                raise IntegrityError(f"role {role.decision_id} references missing experiment")
            if not available(
                "configurations", "configuration_id", role.configuration_id, config_ids
            ):
                raise IntegrityError(f"role {role.decision_id} references missing configuration")

    @staticmethod
    def _topological_experiments(
        experiments: Sequence[ExperimentRecord],
    ) -> tuple[ExperimentRecord, ...]:
        remaining = {item.experiment_id: item for item in experiments}
        ordered: list[ExperimentRecord] = []
        while remaining:
            ready = sorted(
                (
                    item
                    for item in remaining.values()
                    if item.parent_experiment_id is None
                    or item.parent_experiment_id not in remaining
                ),
                key=lambda item: item.experiment_id,
            )
            if not ready:
                raise IntegrityError("experiment parent graph contains a cycle")
            for item in ready:
                ordered.append(item)
                remaining.pop(item.experiment_id)
        return tuple(ordered)
