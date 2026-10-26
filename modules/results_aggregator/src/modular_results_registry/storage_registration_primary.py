from __future__ import annotations

import sqlite3

from .canonical import canonical_json_bytes
from .models import RegistrationPackage
from .storage_registration_groups import _RegistrationAccumulator

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationPrimaryMixin(StorageMixinBase):
    def _register_primary_entities(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        for artifact in sorted(package.artifacts, key=lambda item: item.artifact_id):
            payload = canonical_json_bytes(artifact)
            added = self._insert_immutable(
                connection,
                table="artifacts",
                id_column="artifact_id",
                entity_type="artifact",
                entity_id=artifact.artifact_id,
                content_hash=artifact.content_hash,
                payload=payload,
                columns=(
                    "artifact_id",
                    "package_id",
                    "sha256",
                    "content_hash",
                    "payload_json",
                    "status",
                    "registered_revision",
                ),
                values=(
                    artifact.artifact_id,
                    artifact.package_id,
                    artifact.sha256,
                    artifact.content_hash,
                    payload,
                    artifact.status.value,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                (
                    "artifact",
                    artifact.artifact_id,
                    artifact.content_hash,
                    artifact.status.value,
                ),
            )

        for dataset in sorted(package.datasets, key=lambda item: item.dataset_id):
            payload = canonical_json_bytes(dataset)
            added = self._insert_immutable(
                connection,
                table="datasets",
                id_column="dataset_id",
                entity_type="dataset",
                entity_id=dataset.dataset_id,
                content_hash=dataset.content_hash,
                payload=payload,
                columns=(
                    "dataset_id",
                    "sha256",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    dataset.dataset_id,
                    dataset.sha256,
                    dataset.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                ("dataset", dataset.dataset_id, dataset.content_hash, "REGISTERED"),
            )

        for config in sorted(package.configurations, key=lambda item: item.configuration_id):
            payload = canonical_json_bytes(config)
            added = self._insert_immutable(
                connection,
                table="configurations",
                id_column="configuration_id",
                entity_type="configuration",
                entity_id=config.configuration_id,
                content_hash=config.content_hash,
                payload=payload,
                columns=(
                    "configuration_id",
                    "strategy_id",
                    "configuration_hash",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    config.configuration_id,
                    config.strategy_id,
                    config.configuration_hash,
                    config.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                (
                    "configuration",
                    config.configuration_id,
                    config.content_hash,
                    "REGISTERED",
                ),
            )

        for experiment in self._topological_experiments(package.experiments):
            payload = canonical_json_bytes(experiment)
            added = self._insert_immutable(
                connection,
                table="experiments",
                id_column="experiment_id",
                entity_type="experiment",
                entity_id=experiment.experiment_id,
                content_hash=experiment.content_hash,
                payload=payload,
                columns=(
                    "experiment_id",
                    "research_id",
                    "parent_experiment_id",
                    "strategy_id",
                    "status",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    experiment.experiment_id,
                    experiment.research_id,
                    experiment.parent_experiment_id,
                    experiment.strategy_id,
                    experiment.status.value,
                    experiment.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                (
                    "experiment",
                    experiment.experiment_id,
                    experiment.content_hash,
                    experiment.status.value,
                ),
            )
