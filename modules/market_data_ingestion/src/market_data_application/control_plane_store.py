from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest
from hmac import new as new_hmac
from pathlib import Path
from threading import RLock
from typing import Any

from .control_plane_security import ApiTokenSnapshot, TokenSnapshotState

_FORBIDDEN_DETAIL_KEYS = {
    "authorization",
    "bearer",
    "credential",
    "password",
    "plaintext_token",
    "secret",
    "token",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _utc(datetime.fromisoformat(normalized))


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _state_mapping(state: TokenSnapshotState) -> dict[str, object]:
    return {
        "current": state.current.to_mapping(),
        "previous": state.previous.to_mapping() if state.previous is not None else None,
        "previous_expires_at": (
            _timestamp(state.previous_expires_at) if state.previous_expires_at is not None else None
        ),
    }


def _state_from_mapping(value: Mapping[str, object]) -> TokenSnapshotState:
    current = value.get("current")
    previous = value.get("previous")
    previous_expires_at = value.get("previous_expires_at")
    if not isinstance(current, dict):
        raise ValueError("stored current token snapshot is invalid")
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("stored previous token snapshot is invalid")
    if previous_expires_at is not None and not isinstance(previous_expires_at, str):
        raise ValueError("stored previous token expiry is invalid")
    return TokenSnapshotState(
        current=ApiTokenSnapshot.from_mapping(current),
        previous=ApiTokenSnapshot.from_mapping(previous) if previous is not None else None,
        previous_expires_at=(
            _parse_timestamp(previous_expires_at) if previous_expires_at is not None else None
        ),
    )


def _state_fingerprint(serialized: str) -> str:
    return sha256(serialized.encode("utf-8")).hexdigest()


def _validate_identifier(value: str, *, name: str, max_length: int = 256) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{name} must contain between 1 and {max_length} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{name} must not contain control characters")
    return normalized


def _validate_details(value: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        key_text = _validate_identifier(str(key), name="audit detail key", max_length=128)
        if key_text.lower() in _FORBIDDEN_DETAIL_KEYS:
            raise ValueError(f"audit detail key is forbidden: {key_text}")
        if isinstance(item, (str, int, float, bool)) or item is None:
            if isinstance(item, str) and len(item) > 1024:
                raise ValueError("audit detail string exceeds 1024 characters")
            normalized[key_text] = item
            continue
        if isinstance(item, list) and all(
            isinstance(entry, (str, int, float, bool)) or entry is None for entry in item
        ):
            normalized[key_text] = list(item)
            continue
        raise TypeError("audit details must contain bounded JSON scalar values or scalar lists")
    return normalized


@dataclass(frozen=True, slots=True)
class AuditContext:
    timestamp: datetime
    correlation_id: str
    direct_peer: str
    action: str
    outcome: str
    principal_id: str | None = None
    token_id: str | None = None
    details: Mapping[str, object] | None = None

    def normalized(self) -> AuditContext:
        return AuditContext(
            timestamp=_utc(self.timestamp),
            correlation_id=_validate_identifier(
                self.correlation_id,
                name="correlation_id",
                max_length=128,
            ),
            direct_peer=_validate_identifier(self.direct_peer, name="direct_peer", max_length=255),
            action=_validate_identifier(self.action, name="audit action", max_length=128),
            outcome=_validate_identifier(self.outcome, name="audit outcome", max_length=64),
            principal_id=(
                _validate_identifier(self.principal_id, name="principal_id", max_length=128)
                if self.principal_id is not None
                else None
            ),
            token_id=(
                _validate_identifier(self.token_id, name="token_id", max_length=128)
                if self.token_id is not None
                else None
            ),
            details=_validate_details(self.details or {}),
        )


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    timestamp: datetime
    correlation_id: str
    direct_peer: str
    principal_id: str | None
    token_id: str | None
    action: str
    outcome: str
    details: Mapping[str, object]
    previous_mac: str
    record_mac: str


@dataclass(frozen=True, slots=True)
class AuditChainStatus:
    valid: bool
    record_count: int
    head_mac: str


class ControlPlaneStateError(RuntimeError):
    pass


class AuditIntegrityError(ControlPlaneStateError):
    pass


class SQLiteControlPlaneStore:
    """Durable token state and HMAC-chained administrative audit."""

    def __init__(self, path: Path, *, audit_key: bytes) -> None:
        self.path = path.expanduser().resolve()
        if len(audit_key) < 32:
            raise ValueError("audit key must contain at least 32 bytes")
        self._audit_key = bytes(audit_key)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._initialize_schema(connection)
            self._verify_chain(connection)
            self._load_state_row(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 30000")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_plane_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                generation INTEGER NOT NULL CHECK (generation > 0),
                state_json TEXT NOT NULL,
                state_fingerprint TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS control_plane_audit (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                direct_peer TEXT NOT NULL,
                principal_id TEXT,
                token_id TEXT,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                details_json TEXT NOT NULL,
                previous_mac TEXT NOT NULL,
                record_mac TEXT NOT NULL UNIQUE
            );
            """
        )

    def bootstrap(self, state: TokenSnapshotState, *, audit: AuditContext) -> TokenSnapshotState:
        serialized = _canonical_json(_state_mapping(state))
        fingerprint = _state_fingerprint(serialized)
        normalized_audit = audit.normalized()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT generation, state_json, state_fingerprint "
                "FROM control_plane_state WHERE singleton = 1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO control_plane_state "
                    "(singleton, generation, state_json, state_fingerprint) VALUES (1, ?, ?, ?)",
                    (state.current.generation, serialized, fingerprint),
                )
                self._append_audit(connection, normalized_audit)
                connection.commit()
                return state
            loaded = self._decode_state_row(existing)
            stored_fingerprint = str(existing["state_fingerprint"])
            if state.current.generation < loaded.current.generation:
                raise ControlPlaneStateError("bootstrap token generation would roll back state")
            if (
                state.current.generation != loaded.current.generation
                or fingerprint != stored_fingerprint
            ):
                raise ControlPlaneStateError(
                    "stored token state differs from bootstrap state; explicit reload is required"
                )
            connection.rollback()
            return loaded

    def load_state(self) -> TokenSnapshotState:
        with self._lock, self._connect() as connection:
            row = self._load_state_row(connection)
            if row is None:
                raise ControlPlaneStateError("control-plane token state is not initialized")
            return self._decode_state_row(row)

    def replace_state(
        self,
        state: TokenSnapshotState,
        *,
        expected_generation: int,
        audit: AuditContext,
    ) -> TokenSnapshotState:
        serialized = _canonical_json(_state_mapping(state))
        fingerprint = _state_fingerprint(serialized)
        normalized_audit = audit.normalized()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_chain(connection)
            row = self._load_state_row(connection)
            if row is None:
                raise ControlPlaneStateError("control-plane token state is not initialized")
            current_generation = int(row["generation"])
            if current_generation != expected_generation:
                raise ControlPlaneStateError("token state changed before the mutation committed")
            if state.current.generation <= current_generation:
                raise ControlPlaneStateError("token generation must increase monotonically")
            connection.execute(
                "UPDATE control_plane_state SET generation = ?, state_json = ?, "
                "state_fingerprint = ? WHERE singleton = 1 AND generation = ?",
                (
                    state.current.generation,
                    serialized,
                    fingerprint,
                    expected_generation,
                ),
            )
            if connection.total_changes != 1:
                raise ControlPlaneStateError("token state compare-and-swap failed")
            self._append_audit(connection, normalized_audit)
            connection.commit()
            return state

    def append_audit(self, audit: AuditContext) -> AuditRecord:
        normalized = audit.normalized()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_chain(connection)
            record = self._append_audit(connection, normalized)
            connection.commit()
            return record

    def verify_audit_chain(self) -> AuditChainStatus:
        with self._lock, self._connect() as connection:
            return self._verify_chain(connection)

    def audit_records(self, *, limit: int = 1000) -> tuple[AuditRecord, ...]:
        if not 1 <= limit <= 10_000:
            raise ValueError("audit record limit must be between 1 and 10000")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM control_plane_audit ORDER BY sequence ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    @staticmethod
    def _load_state_row(connection: sqlite3.Connection) -> sqlite3.Row | None:
        fetched = connection.execute(
            "SELECT generation, state_json, state_fingerprint "
            "FROM control_plane_state WHERE singleton = 1"
        ).fetchone()
        if fetched is None:
            return None
        if not isinstance(fetched, sqlite3.Row):
            raise AuditIntegrityError("SQLite state query did not return a Row")
        row = fetched
        serialized = str(row["state_json"])
        observed = _state_fingerprint(serialized)
        expected = str(row["state_fingerprint"])
        if not compare_digest(observed, expected):
            raise AuditIntegrityError("stored token-state fingerprint is invalid")
        return row

    @staticmethod
    def _decode_state_row(row: sqlite3.Row) -> TokenSnapshotState:
        raw = json.loads(str(row["state_json"]))
        if not isinstance(raw, dict):
            raise AuditIntegrityError("stored token state must be a JSON object")
        state = _state_from_mapping(raw)
        if state.current.generation != int(row["generation"]):
            raise AuditIntegrityError("stored token generation does not match state payload")
        return state

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        audit: AuditContext,
    ) -> AuditRecord:
        previous = connection.execute(
            "SELECT sequence, record_mac FROM control_plane_audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_mac = str(previous["record_mac"]) if previous is not None else "0" * 64
        next_sequence = int(previous["sequence"]) + 1 if previous is not None else 1
        details = dict(audit.details or {})
        payload = {
            "sequence": next_sequence,
            "timestamp_utc": _timestamp(audit.timestamp),
            "correlation_id": audit.correlation_id,
            "direct_peer": audit.direct_peer,
            "principal_id": audit.principal_id,
            "token_id": audit.token_id,
            "action": audit.action,
            "outcome": audit.outcome,
            "details": details,
            "previous_mac": previous_mac,
        }
        record_mac = new_hmac(
            self._audit_key,
            _canonical_json(payload).encode("utf-8"),
            "sha256",
        ).hexdigest()
        cursor = connection.execute(
            "INSERT INTO control_plane_audit "
            "(timestamp_utc, correlation_id, direct_peer, principal_id, token_id, "
            "action, outcome, details_json, previous_mac, record_mac) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["timestamp_utc"],
                audit.correlation_id,
                audit.direct_peer,
                audit.principal_id,
                audit.token_id,
                audit.action,
                audit.outcome,
                _canonical_json(details),
                previous_mac,
                record_mac,
            ),
        )
        if cursor.lastrowid is None:
            raise AuditIntegrityError("audit insert did not return a sequence")
        sequence = int(cursor.lastrowid)
        if sequence != next_sequence:
            raise AuditIntegrityError("audit sequence is not contiguous")
        return AuditRecord(
            sequence=sequence,
            timestamp=audit.timestamp,
            correlation_id=audit.correlation_id,
            direct_peer=audit.direct_peer,
            principal_id=audit.principal_id,
            token_id=audit.token_id,
            action=audit.action,
            outcome=audit.outcome,
            details=details,
            previous_mac=previous_mac,
            record_mac=record_mac,
        )

    def _verify_chain(self, connection: sqlite3.Connection) -> AuditChainStatus:
        rows = connection.execute(
            "SELECT * FROM control_plane_audit ORDER BY sequence ASC"
        ).fetchall()
        previous_mac = "0" * 64
        expected_sequence = 1
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != expected_sequence:
                raise AuditIntegrityError("audit sequence is not contiguous")
            stored_previous = str(row["previous_mac"])
            if not compare_digest(stored_previous, previous_mac):
                raise AuditIntegrityError("audit previous MAC does not match chain head")
            details = json.loads(str(row["details_json"]))
            if not isinstance(details, dict):
                raise AuditIntegrityError("audit details must be a JSON object")
            payload: dict[str, Any] = {
                "sequence": sequence,
                "timestamp_utc": str(row["timestamp_utc"]),
                "correlation_id": str(row["correlation_id"]),
                "direct_peer": str(row["direct_peer"]),
                "principal_id": row["principal_id"],
                "token_id": row["token_id"],
                "action": str(row["action"]),
                "outcome": str(row["outcome"]),
                "details": details,
                "previous_mac": previous_mac,
            }
            expected_mac = new_hmac(
                self._audit_key,
                _canonical_json(payload).encode("utf-8"),
                "sha256",
            ).hexdigest()
            stored_mac = str(row["record_mac"])
            if not compare_digest(expected_mac, stored_mac):
                raise AuditIntegrityError("audit record MAC is invalid")
            previous_mac = stored_mac
            expected_sequence += 1
        return AuditChainStatus(
            valid=True,
            record_count=len(rows),
            head_mac=previous_mac,
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> AuditRecord:
        details = json.loads(str(row["details_json"]))
        if not isinstance(details, dict):
            raise AuditIntegrityError("audit details must be a JSON object")
        return AuditRecord(
            sequence=int(row["sequence"]),
            timestamp=_parse_timestamp(str(row["timestamp_utc"])),
            correlation_id=str(row["correlation_id"]),
            direct_peer=str(row["direct_peer"]),
            principal_id=(str(row["principal_id"]) if row["principal_id"] is not None else None),
            token_id=str(row["token_id"]) if row["token_id"] is not None else None,
            action=str(row["action"]),
            outcome=str(row["outcome"]),
            details=details,
            previous_mac=str(row["previous_mac"]),
            record_mac=str(row["record_mac"]),
        )
