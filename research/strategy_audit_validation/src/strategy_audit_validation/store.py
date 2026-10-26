"""Durable content-addressed evidence and tamper-evident decision storage."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contracts import CONTRACT_VERSION, AuditDecision, AuditOutcome, AuditRequestBinding
from .errors import EvidenceIdentityConflictError, EvidenceTamperedError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 2


class SQLiteAuditStore:
    """Append-only audit evidence and decision chain.

    The SQLite database stores no signing secret. Each decision is content-bound
    to evidence and chained to the prior decision record. Every public read/write
    path is preceded or followed by a complete integrity check by the auditor.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._initialize()
        except Exception:
            self._connection.close()
            raise
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")

    def _initialize(self) -> None:
        version = self._schema_version()
        tables = self._tables()
        if version is None and tables:
            raise EvidenceTamperedError("audit store has tables but no schema version")
        if version not in (None, 1, _SCHEMA_VERSION):
            raise EvidenceTamperedError("unsupported or unexpected audit store metadata")
        if version == 1:
            if "audit_request_bindings" in tables:
                raise EvidenceTamperedError("v1 audit store unexpectedly contains request bindings")
            self._validate_table_shapes(include_bindings=False)
        elif version == _SCHEMA_VERSION:
            self._validate_table_shapes(include_bindings=True)

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            locked_version = self._schema_version()
            if locked_version != version:
                raise EvidenceTamperedError("audit store schema changed during initialization")
            if version is None:
                if self._tables():
                    raise EvidenceTamperedError("audit store has tables but no schema version")
                self._create_base_schema()
                self._create_binding_schema()
                self._connection.execute(
                    "INSERT INTO audit_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif version == 1:
                if "audit_request_bindings" in self._tables():
                    raise EvidenceTamperedError(
                        "v1 audit store unexpectedly contains request bindings"
                    )
                self._validate_table_shapes(include_bindings=False)
                self._create_binding_schema()
                self._connection.execute(
                    "UPDATE audit_metadata SET value = ? "
                    "WHERE key = 'schema_version' AND value = '1'",
                    (str(_SCHEMA_VERSION),),
                )
                if self._connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise EvidenceTamperedError("audit store schema changed during migration")
            else:
                # A v2 store with a missing binding table is corrupt, not a repair target.
                self._validate_table_shapes(include_bindings=True)
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def _tables(self) -> set[str]:
        return {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    def _schema_version(self) -> int | None:
        tables = self._tables()
        if "audit_metadata" not in tables:
            return None
        metadata = self._connection.execute(
            "SELECT key, value FROM audit_metadata ORDER BY key"
        ).fetchall()
        if len(metadata) != 1 or metadata[0]["key"] != "schema_version":
            raise EvidenceTamperedError("unsupported or unexpected audit store metadata")
        try:
            version = int(metadata[0]["value"])
        except (TypeError, ValueError) as exc:
            raise EvidenceTamperedError("unsupported or unexpected audit store metadata") from exc
        if str(version) != metadata[0]["value"]:
            raise EvidenceTamperedError("unsupported or unexpected audit store metadata")
        return version

    def _validate_table_shapes(self, *, include_bindings: bool) -> None:
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
        if include_bindings:
            required["audit_request_bindings"] = {
                "audit_id",
                "schema_version",
                "request_hash",
                "policy_fingerprint",
                "decision_hash",
                "signature",
            }
        tables = self._tables()
        for table, columns in required.items():
            if table not in tables:
                raise EvidenceTamperedError(f"audit table is missing: {table}")
            actual = {
                str(row["name"])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns <= actual:
                raise EvidenceTamperedError(f"audit table schema is incompatible: {table}")

    def _create_base_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE audit_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE audit_evidence (reference TEXT PRIMARY KEY, evidence_hash TEXT NOT NULL, "
            "payload_json BLOB NOT NULL, candidate_id TEXT NOT NULL, first_seen_at TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE audit_decisions (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "request_hash TEXT NOT NULL UNIQUE, audit_id TEXT NOT NULL UNIQUE, "
            "candidate_id TEXT NOT NULL, outcome TEXT NOT NULL, evidence_reference TEXT NOT NULL, "
            "evidence_hash TEXT NOT NULL, reason_codes_json TEXT NOT NULL, "
            "issued_at TEXT NOT NULL, "
            "auditor_id TEXT NOT NULL, contract_version TEXT NOT NULL, signature TEXT NOT NULL, "
            "decision_hash TEXT NOT NULL, previous_record_hash TEXT, record_hash TEXT NOT NULL, "
            "FOREIGN KEY(evidence_reference) REFERENCES audit_evidence(reference))"
        )

    def _create_binding_schema(self) -> None:
        self._connection.execute(
            "CREATE TABLE audit_request_bindings ("
            "audit_id TEXT PRIMARY KEY REFERENCES audit_decisions(audit_id), "
            "schema_version INTEGER NOT NULL, request_hash TEXT NOT NULL UNIQUE "
            "REFERENCES audit_decisions(request_hash), policy_fingerprint TEXT NOT NULL, "
            "decision_hash TEXT NOT NULL, signature TEXT NOT NULL)"
        )

    def register_evidence(
        self,
        *,
        reference: str,
        payload: Mapping[str, Any],
        candidate_id: str,
        observed_at: datetime,
    ) -> str:
        if (
            not isinstance(reference, str)
            or not reference.strip()
            or reference != reference.strip()
        ):
            raise ValueError("evidence reference must be a non-blank trimmed string")
        encoded = canonical_json_bytes(payload)
        digest = canonical_sha256(payload)
        timestamp = _iso(observed_at)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                """
                SELECT evidence_hash, payload_json, candidate_id
                FROM audit_evidence WHERE reference = ?
                """,
                (reference,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["evidence_hash"] != digest
                    or bytes(existing["payload_json"]) != encoded
                    or existing["candidate_id"] != candidate_id
                ):
                    raise EvidenceIdentityConflictError(
                        "evidence reference was reused with different content"
                    )
                self._connection.execute("COMMIT")
                return digest
            self._connection.execute(
                """
                INSERT INTO audit_evidence(
                    reference, evidence_hash, payload_json, candidate_id, first_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (reference, digest, encoded, candidate_id, timestamp),
            )
            self._connection.execute("COMMIT")
            return digest
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def decision_for_request(self, request_hash: str) -> AuditDecision | None:
        _digest(request_hash, "request_hash")
        row = self._connection.execute(
            "SELECT * FROM audit_decisions WHERE request_hash = ?", (request_hash,)
        ).fetchone()
        return None if row is None else _decision(row)

    def _decision_by_audit_id(self, audit_id: str) -> AuditDecision | None:
        """Load one existing decision; caller must verify the full chain first."""

        if not isinstance(audit_id, str) or not audit_id or audit_id.strip() != audit_id:
            raise ValueError("audit_id must be a non-blank trimmed string")
        if self.path.is_symlink() or not self.path.is_file():
            raise sqlite3.OperationalError("audit store is unavailable")
        row = self._connection.execute(
            "SELECT * FROM audit_decisions WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        return None if row is None else _decision(row)

    def _request_binding_by_audit_id(self, audit_id: str) -> AuditRequestBinding | None:
        row = self._connection.execute(
            "SELECT * FROM audit_request_bindings WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return AuditRequestBinding(
                audit_id=str(row["audit_id"]),
                request_hash=str(row["request_hash"]),
                policy_fingerprint=str(row["policy_fingerprint"]),
                decision_hash=str(row["decision_hash"]),
                signature=str(row["signature"]),
                schema_version=int(row["schema_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise EvidenceTamperedError("stored audit request binding is malformed") from exc

    def _stored_request_hash_by_audit_id(self, audit_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT request_hash FROM audit_decisions WHERE audit_id = ?", (audit_id,)
        ).fetchone()
        return None if row is None else str(row["request_hash"])

    def save_decision(
        self,
        *,
        request_hash: str,
        decision: AuditDecision,
    ) -> AuditDecision:
        """Persist an explicitly unbound legacy decision row.

        New auditor-owned decisions must use :meth:`save_bound_decision`. This
        compatibility method remains for legacy importers and fixtures; rows
        written here can never pass strong request-binding verification.
        """

        return self.save_legacy_unbound_decision(request_hash=request_hash, decision=decision)

    def save_bound_decision(
        self,
        *,
        request_hash: str,
        decision: AuditDecision,
        binding: AuditRequestBinding,
    ) -> AuditDecision:
        """Atomically persist a decision and its signer-authenticated request binding."""

        return self._save_decision(request_hash=request_hash, decision=decision, binding=binding)

    def save_legacy_unbound_decision(
        self, *, request_hash: str, decision: AuditDecision
    ) -> AuditDecision:
        """Explicitly persist a legacy/unbound row for compatibility fixtures/imports."""

        return self._save_decision(request_hash=request_hash, decision=decision, binding=None)

    def _save_decision(
        self,
        *,
        request_hash: str,
        decision: AuditDecision,
        binding: AuditRequestBinding | None,
    ) -> AuditDecision:
        _digest(request_hash, "request_hash")
        if binding is not None and (
            binding.request_hash != request_hash
            or binding.audit_id != decision.audit_id
            or binding.decision_hash != decision.decision_hash
        ):
            raise EvidenceIdentityConflictError("request binding does not match decision identity")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT * FROM audit_decisions WHERE request_hash = ? OR audit_id = ?",
                (request_hash, decision.audit_id),
            ).fetchone()
            if existing is not None:
                restored = _decision(existing)
                if (
                    existing["request_hash"] != request_hash
                    or restored.decision_hash != decision.decision_hash
                ):
                    raise EvidenceIdentityConflictError(
                        "audit request or audit_id was reused with different content"
                    )
                existing_binding = self._request_binding_by_audit_id(decision.audit_id)
                if binding is not None and existing_binding != binding:
                    raise EvidenceTamperedError("stored request binding does not match replay")
                self._connection.execute("COMMIT")
                return restored
            evidence = self._connection.execute(
                "SELECT evidence_hash, candidate_id FROM audit_evidence WHERE reference = ?",
                (decision.evidence_package_reference,),
            ).fetchone()
            if evidence is None:
                raise EvidenceTamperedError("decision evidence is missing")
            if (
                evidence["evidence_hash"] != decision.evidence_hash
                or evidence["candidate_id"] != decision.candidate_id
            ):
                raise EvidenceTamperedError("decision and durable evidence identity differ")
            previous = self._connection.execute(
                "SELECT record_hash FROM audit_decisions ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = None if previous is None else str(previous["record_hash"])
            record_hash = canonical_sha256(
                {
                    "request_hash": request_hash,
                    "decision_hash": decision.decision_hash,
                    "previous_record_hash": previous_hash,
                }
            )
            self._connection.execute(
                """
                INSERT INTO audit_decisions(
                    request_hash, audit_id, candidate_id, outcome, evidence_reference,
                    evidence_hash, reason_codes_json, issued_at, auditor_id,
                    contract_version, signature, decision_hash, previous_record_hash,
                    record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_hash,
                    decision.audit_id,
                    decision.candidate_id,
                    decision.outcome.value,
                    decision.evidence_package_reference,
                    decision.evidence_hash,
                    canonical_json_bytes(decision.reason_codes).decode("utf-8"),
                    _iso(decision.issued_at),
                    decision.auditor_id,
                    decision.contract_version,
                    decision.signature,
                    decision.decision_hash,
                    previous_hash,
                    record_hash,
                ),
            )
            if binding is not None:
                self._connection.execute(
                    """INSERT INTO audit_request_bindings(
                        audit_id, schema_version, request_hash, policy_fingerprint,
                        decision_hash, signature
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        binding.audit_id,
                        binding.schema_version,
                        binding.request_hash,
                        binding.policy_fingerprint,
                        binding.decision_hash,
                        binding.signature,
                    ),
                )
            self._connection.execute("COMMIT")
            return decision
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def verify_integrity(self) -> None:
        """Verify every evidence row and the complete append-only decision chain."""

        evidence_by_reference: dict[str, tuple[str, str]] = {}
        evidence_rows = self._connection.execute(
            "SELECT * FROM audit_evidence ORDER BY reference"
        ).fetchall()
        for row in evidence_rows:
            reference = str(row["reference"])
            digest = _digest(str(row["evidence_hash"]), "stored evidence_hash")
            candidate_id = str(row["candidate_id"])
            _parse_utc(str(row["first_seen_at"]), "stored first_seen_at")
            payload_bytes = bytes(row["payload_json"])
            try:
                decoded = strict_json_loads(payload_bytes)
                recoded = canonical_json_bytes(decoded)
            except (TypeError, ValueError) as exc:
                raise EvidenceTamperedError("stored evidence JSON is invalid") from exc
            if recoded != payload_bytes or canonical_sha256(decoded) != digest:
                raise EvidenceTamperedError("stored evidence content hash mismatch")
            if not isinstance(decoded, Mapping):
                raise EvidenceTamperedError("stored evidence root is not a mapping")
            candidate = decoded.get("candidate")
            if not isinstance(candidate, Mapping) or candidate.get("candidate_id") != candidate_id:
                raise EvidenceTamperedError("stored evidence candidate identity mismatch")
            evidence_by_reference[reference] = (digest, candidate_id)

        previous: str | None = None
        decision_rows = self._connection.execute(
            "SELECT * FROM audit_decisions ORDER BY sequence"
        ).fetchall()
        expected_sequence = 1
        for row in decision_rows:
            if row["sequence"] != expected_sequence:
                raise EvidenceTamperedError("audit decision sequence is not contiguous")
            expected_sequence += 1
            request_hash = _digest(str(row["request_hash"]), "stored request_hash")
            decision = _decision(row)
            if row["previous_record_hash"] != previous:
                raise EvidenceTamperedError("audit decision chain linkage mismatch")
            expected_record = canonical_sha256(
                {
                    "request_hash": request_hash,
                    "decision_hash": decision.decision_hash,
                    "previous_record_hash": previous,
                }
            )
            record_hash = _digest(str(row["record_hash"]), "stored record_hash")
            if record_hash != expected_record:
                raise EvidenceTamperedError("audit decision chain record hash mismatch")
            durable = evidence_by_reference.get(decision.evidence_package_reference)
            if durable is None:
                raise EvidenceTamperedError("audit decision evidence is missing")
            if durable != (decision.evidence_hash, decision.candidate_id):
                raise EvidenceTamperedError("decision is bound to different stored evidence")
            previous = record_hash

    def verify_chain(self) -> None:
        self.verify_integrity()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteAuditStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


def _decision(row: sqlite3.Row) -> AuditDecision:
    try:
        issued_at = _parse_utc(str(row["issued_at"]), "stored issued_at")
        reasons_raw = strict_json_loads(str(row["reason_codes_json"]))
        if not isinstance(reasons_raw, list) or any(
            not isinstance(item, str) for item in reasons_raw
        ):
            raise EvidenceTamperedError("stored decision reason codes are malformed")
        return AuditDecision(
            audit_id=str(row["audit_id"]),
            candidate_id=str(row["candidate_id"]),
            outcome=AuditOutcome(str(row["outcome"])),
            evidence_package_reference=str(row["evidence_reference"]),
            evidence_hash=str(row["evidence_hash"]),
            reason_codes=tuple(reasons_raw),
            issued_at=issued_at,
            auditor_id=str(row["auditor_id"]),
            contract_version=str(row["contract_version"]),
            signature=str(row["signature"]),
            decision_hash=str(row["decision_hash"]),
        )
    except EvidenceTamperedError:
        raise
    except (TypeError, ValueError) as exc:
        raise EvidenceTamperedError("stored audit decision is malformed") from exc


def _digest(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise EvidenceTamperedError(f"{name} is not a lowercase SHA-256")
    return value


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit store timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str, name: str) -> datetime:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise EvidenceTamperedError(f"{name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceTamperedError(f"{name} is not timezone-aware")
    return parsed.astimezone(UTC)


__all__ = ["CONTRACT_VERSION", "SQLiteAuditStore"]
