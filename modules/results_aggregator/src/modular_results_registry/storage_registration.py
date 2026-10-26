from __future__ import annotations

from datetime import datetime, timezone

from .canonical import canonical_json_bytes, deterministic_id
from .errors import ConflictError, ResourceLimitError
from .models import RegistrationPackage
from .storage_common import RegisterOutcome, _ConflictDetails, _DetectedConflict
from .storage_registration_groups import _RegistrationAccumulator

from .storage_mixin_base import StorageMixinBase


class StorageRegistrationMixin(StorageMixinBase):
    def register_package(self, package: RegistrationPackage) -> RegisterOutcome:
        if not isinstance(package, RegistrationPackage):
            raise TypeError("package must be a RegistrationPackage")
        from . import storage as storage_facade

        entity_count = self._count_package_entities(package)
        if entity_count > storage_facade._MAX_REGISTRATION_ENTITIES:
            raise ResourceLimitError("registration package exceeds the entity limit")
        payload = canonical_json_bytes(package)
        if len(payload) > storage_facade._MAX_REGISTRATION_PACKAGE_METADATA_BYTES:
            raise ResourceLimitError("registration package metadata exceeds its byte limit")
        package_hash = package.content_hash
        try:
            with self.transaction() as connection:
                existing_package = self._existing_hash(
                    connection,
                    table="packages",
                    id_column="package_id",
                    entity_id=package.package_id,
                )
                if existing_package is not None:
                    if existing_package == package_hash:
                        return RegisterOutcome(
                            package.package_id,
                            "DUPLICATE",
                            self.revision(),
                            0,
                            entity_count,
                        )
                    raise _DetectedConflict(
                        _ConflictDetails(
                            entity_type="package",
                            entity_id=package.package_id,
                            existing_content_hash=existing_package,
                            incoming_content_hash=package_hash,
                            incoming_payload=payload,
                            correlation_id=package.package_id,
                        )
                    )

                self._validate_references(connection, package)
                revision = self._next_revision(connection)
                accumulator = _RegistrationAccumulator()
                self._register_primary_entities(connection, package, revision, accumulator)
                self._register_runs(connection, package, revision, accumulator)
                self._register_metrics(connection, package, revision, accumulator)
                self._register_evidence_and_roles(connection, package, revision, accumulator)

                connection.execute(
                    "INSERT INTO packages("
                    "package_id, content_hash, payload_json, registered_revision"
                    ") VALUES(?, ?, ?, ?)",
                    (package.package_id, package_hash, payload, revision),
                )
                for entity_type, entity_id, content_hash, state in accumulator.pending_audit:
                    self._audit(
                        connection,
                        revision=revision,
                        operation="registered",
                        entity_type=entity_type,
                        entity_id=entity_id,
                        previous_state=None,
                        new_state=state,
                        reason_code="VERIFIED_PACKAGE_REGISTRATION",
                        correlation_id=package.package_id,
                        payload_hash=content_hash,
                    )
                self._audit(
                    connection,
                    revision=revision,
                    operation="package_registered",
                    entity_type="package",
                    entity_id=package.package_id,
                    previous_state=None,
                    new_state="REGISTERED",
                    reason_code="ATOMIC_PACKAGE_COMMIT",
                    correlation_id=package.package_id,
                    payload_hash=package_hash,
                )
                return RegisterOutcome(
                    package.package_id,
                    "REGISTERED",
                    revision,
                    accumulator.registered,
                    accumulator.duplicates,
                )
        except _DetectedConflict as conflict:
            self._commit_quarantine(conflict.details)
            raise ConflictError(
                f"IDENTIFIER_CONTENT_CONFLICT: {conflict.details.entity_type} "
                f"{conflict.details.entity_id}"
            ) from conflict

    @staticmethod
    def _count_package_entities(package: RegistrationPackage) -> int:
        return sum(
            len(items)
            for items in (
                package.artifacts,
                package.datasets,
                package.configurations,
                package.experiments,
                package.runs,
                package.metric_definitions,
                package.metrics,
                package.evidence,
                package.roles,
            )
        )

    def _commit_quarantine(self, details: _ConflictDetails) -> None:
        quarantine_id = deterministic_id(
            "quarantine",
            {
                "entity_type": details.entity_type,
                "entity_id": details.entity_id,
                "incoming_content_hash": details.incoming_content_hash,
            },
            digest_chars=48,
        )
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM quarantine WHERE quarantine_id=?",
                (quarantine_id,),
            ).fetchone()
            if existing is not None:
                return
            revision = self._next_revision(connection)
            connection.execute(
                """
                INSERT INTO quarantine(
                    quarantine_id, entity_type, entity_id, existing_content_hash,
                    incoming_content_hash, payload_json, reason_code, correlation_id,
                    revision, created_at_utc
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quarantine_id,
                    details.entity_type,
                    details.entity_id,
                    details.existing_content_hash,
                    details.incoming_content_hash,
                    details.incoming_payload,
                    "IDENTIFIER_CONTENT_CONFLICT",
                    details.correlation_id,
                    revision,
                    datetime.now(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                ),
            )
            self._audit(
                connection,
                revision=revision,
                operation="quarantined",
                entity_type=details.entity_type,
                entity_id=details.entity_id,
                previous_state="REGISTERED",
                new_state="QUARANTINED",
                reason_code="IDENTIFIER_CONTENT_CONFLICT",
                correlation_id=details.correlation_id,
                payload_hash=details.incoming_content_hash,
            )
