from __future__ import annotations

import sqlite3

from .canonical import canonical_json_bytes
from .models import RegistrationPackage
from .storage_registration_groups import _RegistrationAccumulator

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationMetricsMixin(StorageMixinBase):
    def _register_metrics(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        for definition in sorted(package.metric_definitions, key=lambda item: item.definition_id):
            payload = canonical_json_bytes(definition)
            added = self._insert_immutable(
                connection,
                table="metric_definitions",
                id_column="definition_id",
                entity_type="metric_definition",
                entity_id=definition.definition_id,
                content_hash=definition.content_hash,
                payload=payload,
                columns=(
                    "definition_id",
                    "metric_name",
                    "definition_version",
                    "owner_module",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    definition.definition_id,
                    definition.metric_name,
                    definition.definition_version,
                    definition.owner_module,
                    definition.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                (
                    "metric_definition",
                    definition.definition_id,
                    definition.content_hash,
                    "REGISTERED",
                ),
            )

        for metric in sorted(package.metrics, key=lambda item: item.metric_key):
            payload = canonical_json_bytes(metric)
            added = self._insert_immutable(
                connection,
                table="metrics",
                id_column="metric_key",
                entity_type="metric",
                entity_id=metric.metric_key,
                content_hash=metric.content_hash,
                payload=payload,
                columns=(
                    "metric_key",
                    "run_id",
                    "definition_id",
                    "metric_name",
                    "definition_version",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    metric.metric_key,
                    metric.run_id,
                    metric.definition_id,
                    metric.metric_name,
                    metric.definition_version,
                    metric.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                ("metric", metric.metric_key, metric.content_hash, "REGISTERED"),
            )
