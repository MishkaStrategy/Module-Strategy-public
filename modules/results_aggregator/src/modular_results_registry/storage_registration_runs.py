from __future__ import annotations

import sqlite3

from .canonical import canonical_json_bytes
from .models import RegistrationPackage
from .storage_registration_groups import _RegistrationAccumulator

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationRunsMixin(StorageMixinBase):
    def _register_runs(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        for run in sorted(package.runs, key=lambda item: item.run_id):
            payload = canonical_json_bytes(run)
            added = self._insert_immutable(
                connection,
                table="runs",
                id_column="run_id",
                entity_type="run",
                entity_id=run.run_id,
                content_hash=run.content_hash,
                payload=payload,
                columns=(
                    "run_id",
                    "experiment_id",
                    "configuration_id",
                    "status",
                    "result_hash",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    run.run_id,
                    run.experiment_id,
                    run.configuration_id,
                    run.status.value,
                    run.result_hash,
                    run.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            if added:
                connection.executemany(
                    "INSERT INTO run_datasets(run_id, dataset_id) VALUES(?, ?)",
                    ((run.run_id, item) for item in run.dataset_ids),
                )
                connection.executemany(
                    "INSERT INTO run_artifacts(run_id, artifact_id) VALUES(?, ?)",
                    ((run.run_id, item) for item in run.artifact_ids),
                )
            accumulator.record(
                added,
                ("run", run.run_id, run.content_hash, run.status.value),
            )
