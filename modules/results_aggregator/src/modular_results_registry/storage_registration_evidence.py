from __future__ import annotations

import sqlite3

from .canonical import canonical_json_bytes
from .models import RegistrationPackage
from .storage_common import _ConflictDetails, _DetectedConflict
from .storage_registration_groups import _RegistrationAccumulator

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationEvidenceMixin(StorageMixinBase):
    def _register_evidence_and_roles(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        for evidence in sorted(package.evidence, key=lambda item: item.evidence_id):
            payload = canonical_json_bytes(evidence)
            added = self._insert_immutable(
                connection,
                table="evidence",
                id_column="evidence_id",
                entity_type="evidence",
                entity_id=evidence.evidence_id,
                content_hash=evidence.content_hash,
                payload=payload,
                columns=(
                    "evidence_id",
                    "experiment_id",
                    "evidence_type",
                    "status",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    evidence.evidence_id,
                    evidence.experiment_id,
                    evidence.evidence_type,
                    evidence.status.value,
                    evidence.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            if added:
                connection.executemany(
                    "INSERT INTO evidence_runs(evidence_id, run_id) VALUES(?, ?)",
                    ((evidence.evidence_id, item) for item in evidence.run_ids),
                )
                connection.executemany(
                    "INSERT INTO evidence_artifacts(evidence_id, artifact_id) VALUES(?, ?)",
                    ((evidence.evidence_id, item) for item in evidence.artifact_ids),
                )
            accumulator.record(
                added,
                (
                    "evidence",
                    evidence.evidence_id,
                    evidence.content_hash,
                    evidence.status.value,
                ),
            )

        for role in sorted(package.roles, key=lambda item: item.decision_id):
            existing_role = connection.execute(
                "SELECT decision_id, content_hash FROM experiment_roles "
                "WHERE experiment_id=? AND configuration_id=?",
                (role.experiment_id, role.configuration_id),
            ).fetchone()
            if existing_role is not None and str(existing_role[0]) != role.decision_id:
                raise _DetectedConflict(
                    _ConflictDetails(
                        entity_type="role",
                        entity_id=f"{role.experiment_id}:{role.configuration_id}",
                        existing_content_hash=str(existing_role[1]),
                        incoming_content_hash=role.content_hash,
                        incoming_payload=canonical_json_bytes(role),
                        correlation_id=package.package_id,
                    )
                )
            payload = canonical_json_bytes(role)
            added = self._insert_immutable(
                connection,
                table="experiment_roles",
                id_column="decision_id",
                entity_type="role",
                entity_id=role.decision_id,
                content_hash=role.content_hash,
                payload=payload,
                columns=(
                    "decision_id",
                    "experiment_id",
                    "configuration_id",
                    "role",
                    "content_hash",
                    "payload_json",
                    "registered_revision",
                ),
                values=(
                    role.decision_id,
                    role.experiment_id,
                    role.configuration_id,
                    role.role.value,
                    role.content_hash,
                    payload,
                    revision,
                ),
                correlation_id=package.package_id,
            )
            accumulator.record(
                added,
                ("role", role.decision_id, role.content_hash, role.role.value),
            )
