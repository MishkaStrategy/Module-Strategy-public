"""Side-effect-free query port for independent Strategy Audit durable state."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contracts import AuditDecision, AuditOutcome
from .errors import EvidenceTamperedError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SCHEMA_VERSIONS = {1, 2}
_MAX_INTEGRITY_ROWS = 10_000


class ReadOnlyAuditStoreError(RuntimeError):
    """Raised when an audit ledger cannot be safely projected read-only."""


class ReadOnlyAuditNotFound(ReadOnlyAuditStoreError):
    """Raised when a requested durable audit resource does not exist."""


@dataclass(frozen=True, slots=True)
class AuditIntegritySnapshot:
    evidence_count: int
    decision_count: int
    chain_head: str | None
    hash_chain_verified: bool = True
    hmac_signatures_reverified: bool = False


@dataclass(frozen=True, slots=True)
class AuditEvidenceSnapshot:
    reference: str
    evidence_hash: str
    candidate_id: str
    first_seen_at: datetime
    decision_count: int


@dataclass(frozen=True, slots=True)
class AuditDecisionSnapshot:
    sequence: int
    request_hash: str
    audit_id: str
    candidate_id: str
    outcome: AuditOutcome
    evidence_reference: str
    evidence_hash: str
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str
    contract_version: str
    decision_hash: str
    previous_record_hash: str | None
    record_hash: str
    hmac_signature_present: bool = True
    hmac_signature_reverified: bool = False


class ReadOnlyAuditStore:
    """Read an existing quiescent audit ledger without writable SQLite effects.

    The unkeyed evidence hashes and append-only record chain are fully verified. Historical HMAC
    authenticity is intentionally *not* reverified because the symmetric signing key belongs to
    the independent auditor and must not be copied into a read-only Control Plane.

    Active SQLite sidecars fail closed. The immutable SQLite URI prevents the observer from
    creating WAL/SHM files or taking write-capable locks while reading a quiescent owner database.
    """

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ReadOnlyAuditStoreError("audit database must not be a symlink")
        if not raw.exists():
            raise ReadOnlyAuditStoreError("audit database does not exist")
        if not raw.is_file():
            raise ReadOnlyAuditStoreError("audit database must be a regular file")
        _reject_sqlite_sidecars(raw)
        self.path = raw.resolve(strict=True)
        self._uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
        self._validate_schema()
        self.verify_integrity()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self._uri,
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ReadOnlyAuditStoreError("audit database cannot be opened read-only") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise ReadOnlyAuditStoreError("audit database read failed") from exc
        finally:
            connection.close()

    def _validate_schema(self) -> None:
        required = {
            "audit_metadata": {"key", "value"},
            "audit_evidence": {
                "reference",
                "evidence_hash",
                "payload_json",
                "candidate_id",
                "first_seen_at",
            },
            "audit_decisions": {
                "sequence",
                "request_hash",
                "audit_id",
                "candidate_id",
                "outcome",
                "evidence_reference",
                "evidence_hash",
                "reason_codes_json",
                "issued_at",
                "auditor_id",
                "contract_version",
                "signature",
                "decision_hash",
                "previous_record_hash",
                "record_hash",
            },
        }
        with self._reader() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table, columns in required.items():
                if table not in tables:
                    raise ReadOnlyAuditStoreError(f"audit table is missing: {table}")
                actual = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not columns <= actual:
                    raise ReadOnlyAuditStoreError(f"audit table schema is incompatible: {table}")
            metadata = connection.execute(
                "SELECT key, value FROM audit_metadata ORDER BY key"
            ).fetchall()
            if len(metadata) != 1 or metadata[0]["key"] != "schema_version":
                raise ReadOnlyAuditStoreError("unsupported or unexpected audit store metadata")
            version_raw = metadata[0]["value"]
            try:
                version = int(version_raw)
            except (TypeError, ValueError) as exc:
                raise ReadOnlyAuditStoreError(
                    "unsupported or unexpected audit store metadata"
                ) from exc
            if str(version) != version_raw or version not in _SUPPORTED_SCHEMA_VERSIONS:
                raise ReadOnlyAuditStoreError("unsupported or unexpected audit store metadata")
            if version == 2:
                binding_columns = {
                    "audit_id",
                    "schema_version",
                    "request_hash",
                    "policy_fingerprint",
                    "decision_hash",
                    "signature",
                }
                if "audit_request_bindings" not in tables:
                    raise ReadOnlyAuditStoreError("audit request-binding table is missing")
                actual_binding_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(audit_request_bindings)"
                    ).fetchall()
                }
                if not binding_columns <= actual_binding_columns:
                    raise ReadOnlyAuditStoreError(
                        "audit request-binding table schema is incompatible"
                    )

    def verify_integrity(self) -> AuditIntegritySnapshot:
        with self._reader() as connection:
            evidence_count = self._count(connection, "audit_evidence")
            decision_count = self._count(connection, "audit_decisions")
            if evidence_count > _MAX_INTEGRITY_ROWS or decision_count > _MAX_INTEGRITY_ROWS:
                raise ReadOnlyAuditStoreError("audit ledger exceeds read-only verification limit")

            evidence_by_reference: dict[str, tuple[str, str]] = {}
            evidence_rows = connection.execute(
                "SELECT * FROM audit_evidence ORDER BY reference"
            ).fetchall()
            for row in evidence_rows:
                reference, digest, candidate_id, _ = self._verified_evidence(row)
                evidence_by_reference[reference] = (digest, candidate_id)

            previous: str | None = None
            rows = connection.execute("SELECT * FROM audit_decisions ORDER BY sequence").fetchall()
            expected_sequence = 1
            for row in rows:
                sequence = _positive_integer(row["sequence"], "audit decision sequence")
                if sequence != expected_sequence:
                    raise ReadOnlyAuditStoreError("audit decision sequence is not contiguous")
                expected_sequence += 1
                snapshot = self._verified_decision(row)
                if snapshot.previous_record_hash != previous:
                    raise ReadOnlyAuditStoreError("audit decision chain linkage mismatch")
                expected_record = canonical_sha256(
                    {
                        "request_hash": snapshot.request_hash,
                        "decision_hash": snapshot.decision_hash,
                        "previous_record_hash": previous,
                    }
                )
                if snapshot.record_hash != expected_record:
                    raise ReadOnlyAuditStoreError("audit decision chain record hash mismatch")
                durable = evidence_by_reference.get(snapshot.evidence_reference)
                if durable is None:
                    raise ReadOnlyAuditStoreError("audit decision evidence is missing")
                if durable != (snapshot.evidence_hash, snapshot.candidate_id):
                    raise ReadOnlyAuditStoreError(
                        "audit decision is bound to different stored evidence"
                    )
                previous = snapshot.record_hash
            return AuditIntegritySnapshot(
                evidence_count=evidence_count,
                decision_count=decision_count,
                chain_head=previous,
            )

    def list_decisions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[AuditDecisionSnapshot, ...]:
        _bounded_page(limit=limit, offset=offset)
        self.verify_integrity()
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_decisions ORDER BY sequence DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(self._verified_decision(row) for row in rows)

    def get_decision(self, audit_id: str) -> AuditDecisionSnapshot:
        safe_id = _identifier(audit_id, "audit_id")
        self.verify_integrity()
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM audit_decisions WHERE audit_id = ?",
                (safe_id,),
            ).fetchone()
        if row is None:
            raise ReadOnlyAuditNotFound("audit decision does not exist")
        return self._verified_decision(row)

    def list_evidence(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[AuditEvidenceSnapshot, ...]:
        _bounded_page(limit=limit, offset=offset)
        self.verify_integrity()
        with self._reader() as connection:
            rows = connection.execute(
                """
                SELECT e.*, COUNT(d.sequence) AS decision_count
                FROM audit_evidence AS e
                LEFT JOIN audit_decisions AS d ON d.evidence_reference = e.reference
                GROUP BY e.reference
                ORDER BY e.first_seen_at DESC, e.reference DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return tuple(self._evidence_snapshot(row) for row in rows)

    def get_evidence(self, reference: str) -> AuditEvidenceSnapshot:
        safe_reference = _reference(reference)
        self.verify_integrity()
        with self._reader() as connection:
            row = connection.execute(
                """
                SELECT e.*, COUNT(d.sequence) AS decision_count
                FROM audit_evidence AS e
                LEFT JOIN audit_decisions AS d ON d.evidence_reference = e.reference
                WHERE e.reference = ?
                GROUP BY e.reference
                """,
                (safe_reference,),
            ).fetchone()
        if row is None:
            raise ReadOnlyAuditNotFound("audit evidence does not exist")
        return self._evidence_snapshot(row)

    @staticmethod
    def _count(connection: sqlite3.Connection, table: str) -> int:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        if row is None:
            raise ReadOnlyAuditStoreError("audit count query returned no row")
        return _non_negative_integer(row["count"], "audit row count")

    @staticmethod
    def _verified_evidence(row: sqlite3.Row) -> tuple[str, str, str, datetime]:
        reference = _reference(row["reference"])
        digest = _digest(row["evidence_hash"], "stored evidence_hash")
        candidate_id = _identifier(row["candidate_id"], "candidate_id")
        first_seen_at = _utc_text(row["first_seen_at"], "first_seen_at")
        payload = row["payload_json"]
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ReadOnlyAuditStoreError("stored evidence payload is invalid")
        payload_bytes = bytes(payload)
        try:
            decoded = strict_json_loads(payload_bytes)
            recoded = canonical_json_bytes(decoded)
        except (TypeError, ValueError) as exc:
            raise ReadOnlyAuditStoreError("stored evidence JSON is invalid") from exc
        if recoded != payload_bytes or canonical_sha256(decoded) != digest:
            raise ReadOnlyAuditStoreError("stored evidence content hash mismatch")
        if not isinstance(decoded, dict):
            raise ReadOnlyAuditStoreError("stored evidence root is not an object")
        candidate = decoded.get("candidate")
        if not isinstance(candidate, dict) or candidate.get("candidate_id") != candidate_id:
            raise ReadOnlyAuditStoreError("stored evidence candidate identity mismatch")
        return reference, digest, candidate_id, first_seen_at

    @classmethod
    def _evidence_snapshot(cls, row: sqlite3.Row) -> AuditEvidenceSnapshot:
        reference, digest, candidate_id, first_seen_at = cls._verified_evidence(row)
        return AuditEvidenceSnapshot(
            reference=reference,
            evidence_hash=digest,
            candidate_id=candidate_id,
            first_seen_at=first_seen_at,
            decision_count=_non_negative_integer(row["decision_count"], "decision_count"),
        )

    @staticmethod
    def _verified_decision(row: sqlite3.Row) -> AuditDecisionSnapshot:
        try:
            reason_codes = strict_json_loads(str(row["reason_codes_json"]))
            if not isinstance(reason_codes, list) or any(
                not isinstance(item, str) for item in reason_codes
            ):
                raise ReadOnlyAuditStoreError("stored audit reason codes are malformed")
            decision = AuditDecision(
                audit_id=_identifier(row["audit_id"], "audit_id"),
                candidate_id=_identifier(row["candidate_id"], "candidate_id"),
                outcome=AuditOutcome(str(row["outcome"])),
                evidence_package_reference=_reference(row["evidence_reference"]),
                evidence_hash=_digest(row["evidence_hash"], "evidence_hash"),
                reason_codes=tuple(reason_codes),
                issued_at=_utc_text(row["issued_at"], "issued_at"),
                auditor_id=_identifier(row["auditor_id"], "auditor_id"),
                contract_version=str(row["contract_version"]),
                signature=str(row["signature"]),
                decision_hash=_digest(row["decision_hash"], "decision_hash"),
            )
        except ReadOnlyAuditStoreError:
            raise
        except (EvidenceTamperedError, TypeError, ValueError) as exc:
            raise ReadOnlyAuditStoreError("stored audit decision is malformed or tampered") from exc
        previous = row["previous_record_hash"]
        previous_hash = None if previous is None else _digest(previous, "previous_record_hash")
        return AuditDecisionSnapshot(
            sequence=_positive_integer(row["sequence"], "sequence"),
            request_hash=_digest(row["request_hash"], "request_hash"),
            audit_id=decision.audit_id,
            candidate_id=decision.candidate_id,
            outcome=decision.outcome,
            evidence_reference=decision.evidence_package_reference,
            evidence_hash=decision.evidence_hash,
            reason_codes=decision.reason_codes,
            issued_at=decision.issued_at,
            auditor_id=decision.auditor_id,
            contract_version=decision.contract_version,
            decision_hash=decision.decision_hash,
            previous_record_hash=previous_hash,
            record_hash=_digest(row["record_hash"], "record_hash"),
        )


def _bounded_page(*, limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be non-negative")


def _reject_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ReadOnlyAuditStoreError(
                "audit database has active SQLite sidecars; read-only snapshot is not quiescent"
            )


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
        raise ReadOnlyAuditStoreError(f"{name} is invalid")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ReadOnlyAuditStoreError(f"{name} is invalid")
    return value


def _reference(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ReadOnlyAuditStoreError("evidence reference is invalid")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ReadOnlyAuditStoreError("evidence reference is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReadOnlyAuditStoreError(f"{name} is not a lowercase SHA-256")
    return value


def _utc_text(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ReadOnlyAuditStoreError(f"{name} is invalid")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ReadOnlyAuditStoreError(f"{name} is invalid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadOnlyAuditStoreError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _non_negative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReadOnlyAuditStoreError(f"{name} is invalid")
    return value


def _positive_integer(value: object, name: str) -> int:
    result = _non_negative_integer(value, name)
    if result < 1:
        raise ReadOnlyAuditStoreError(f"{name} must be positive")
    return result


__all__ = [
    "AuditDecisionSnapshot",
    "AuditEvidenceSnapshot",
    "AuditIntegritySnapshot",
    "ReadOnlyAuditNotFound",
    "ReadOnlyAuditStore",
    "ReadOnlyAuditStoreError",
]
