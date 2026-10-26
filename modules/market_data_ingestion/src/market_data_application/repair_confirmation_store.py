from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast, get_args

from market_data_ingestion.canonical import sha256_canonical
from market_data_ingestion.enums import DataType
from market_data_ingestion.repair_planning import RepairAction

from .errors import RepairConfirmationNotFoundError, RepairConfirmationRejectedError
from .repair_confirmation_contracts import RepairConfirmationStatus

TokenFactory = Callable[[int], str]
_REPAIR_ACTION_VALUES = frozenset(get_args(RepairAction))


@dataclass(frozen=True, slots=True)
class RepairConfirmationRecord:
    confirmation_id: str
    dataset_id: str
    version: int
    preview_id: str
    action: RepairAction
    selected_data_type: DataType | None
    acknowledged_mutations: tuple[str, ...]
    actor_id: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    reserved_job_id: str | None = None
    reserved_at: datetime | None = None
    consumed_job_id: str | None = None

    @property
    def status(self) -> RepairConfirmationStatus:
        now = datetime.now(UTC)
        if self.revoked_at is not None:
            return "revoked"
        if self.consumed_at is not None:
            return "consumed"
        if self.expires_at <= now:
            return "expired"
        if self.reserved_job_id is not None:
            return "reserved"
        return "active"


@dataclass(frozen=True, slots=True)
class IssuedRepairConfirmation:
    record: RepairConfirmationRecord
    token: str


class SQLiteRepairConfirmationStore:
    """Durable one-time repair confirmations stored outside the Dataset Registry."""

    def __init__(self, path: Path, *, token_factory: TokenFactory | None = None) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._token_factory = token_factory or secrets.token_urlsafe
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS repair_confirmations (
                    confirmation_id TEXT PRIMARY KEY,
                    token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest) = 64),
                    dataset_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    preview_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    selected_data_type TEXT,
                    acknowledged_mutations_json TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    revoked_at TEXT,
                    reserved_job_id TEXT,
                    reserved_at TEXT,
                    consumed_job_id TEXT,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1)
                );
                CREATE INDEX IF NOT EXISTS idx_repair_confirmations_preview_actor
                    ON repair_confirmations(preview_id, actor_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_repair_confirmations_expiry
                    ON repair_confirmations(expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_confirmations_reserved_job
                    ON repair_confirmations(reserved_job_id)
                    WHERE reserved_job_id IS NOT NULL;
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(repair_confirmations)")
            }
            for name in ("reserved_job_id", "reserved_at", "consumed_job_id"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE repair_confirmations ADD COLUMN {name} TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_repair_confirmations_reserved_job
                ON repair_confirmations(reserved_job_id)
                WHERE reserved_job_id IS NOT NULL
                """
            )

    def issue(
        self,
        *,
        dataset_id: str,
        version: int,
        preview_id: str,
        action: RepairAction,
        selected_data_type: DataType | None,
        acknowledged_mutations: tuple[str, ...],
        actor_id: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> IssuedRepairConfirmation:
        issued_at = self._utc(now or datetime.now(UTC))
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        token = self._token_factory(32)
        if not isinstance(token, str) or len(token) < 32 or len(token) > 256:
            raise ValueError("confirmation token factory returned an invalid token")
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        confirmation_id = (
            "repair-confirmation:"
            + sha256_canonical(
                {
                    "preview_id": preview_id,
                    "actor_id": actor_id,
                    "token_digest": token_digest,
                    "created_at": issued_at,
                }
            )[:24]
        )
        with self._connection(write=True) as connection:
            connection.execute(
                """
                UPDATE repair_confirmations
                SET revoked_at=?
                WHERE preview_id=? AND actor_id=?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                  AND reserved_job_id IS NULL
                """,
                (issued_at.isoformat(), preview_id, actor_id),
            )
            connection.execute(
                """
                INSERT INTO repair_confirmations(
                    confirmation_id, token_digest, dataset_id, version, preview_id,
                    action, selected_data_type, acknowledged_mutations_json, actor_id,
                    created_at, expires_at, consumed_at, revoked_at, reserved_job_id,
                    reserved_at, consumed_job_id, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 1)
                """,
                (
                    confirmation_id,
                    token_digest,
                    dataset_id,
                    version,
                    preview_id,
                    action,
                    selected_data_type.value if selected_data_type is not None else None,
                    json.dumps(list(acknowledged_mutations), separators=(",", ":")),
                    actor_id,
                    issued_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return IssuedRepairConfirmation(record=self.get(confirmation_id), token=token)

    def get(self, confirmation_id: str) -> RepairConfirmationRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repair_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
        if row is None:
            raise RepairConfirmationNotFoundError(confirmation_id)
        return self._record(row)

    def consume(
        self,
        confirmation_id: str,
        *,
        token: str,
        actor_id: str,
        preview_id: str,
        now: datetime | None = None,
    ) -> RepairConfirmationRecord:
        consumed_at = self._utc(now or datetime.now(UTC))
        supplied_digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM repair_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise RepairConfirmationNotFoundError(confirmation_id)
            record = self._record(row)
            reason = self._rejection_reason(
                record,
                actor_id=actor_id,
                preview_id=preview_id,
                at=consumed_at,
            )
            if reason is None and record.reserved_job_id is not None:
                reason = "reserved_for_job"
            if reason is None and not hmac.compare_digest(
                str(row["token_digest"]), supplied_digest
            ):
                reason = "invalid_token"
            if reason is not None:
                raise RepairConfirmationRejectedError(
                    confirmation_id=confirmation_id,
                    reason=reason,
                )
            updated = connection.execute(
                """
                UPDATE repair_confirmations
                SET consumed_at=?
                WHERE confirmation_id=? AND consumed_at IS NULL AND revoked_at IS NULL
                  AND reserved_job_id IS NULL
                """,
                (consumed_at.isoformat(), confirmation_id),
            )
            if updated.rowcount != 1:
                raise RepairConfirmationRejectedError(
                    confirmation_id=confirmation_id,
                    reason="concurrent_consumption",
                )
        return self.get(confirmation_id)

    def consume_reserved(
        self,
        confirmation_id: str,
        *,
        job_id: str,
        actor_id: str,
        preview_id: str,
        now: datetime | None = None,
    ) -> RepairConfirmationRecord:
        consumed_at = self._utc(now or datetime.now(UTC))
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM repair_confirmations WHERE confirmation_id=?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                raise RepairConfirmationNotFoundError(confirmation_id)
            record = self._record(row)
            reason = self._rejection_reason(
                record,
                actor_id=actor_id,
                preview_id=preview_id,
                at=consumed_at,
            )
            if reason == "already_consumed" and record.consumed_job_id == job_id:
                return record
            if reason == "already_consumed":
                reason = "consumed_by_other_job"
            elif reason is None and record.reserved_job_id != job_id:
                reason = "job_reservation_mismatch"
            if reason is not None:
                raise RepairConfirmationRejectedError(
                    confirmation_id=confirmation_id,
                    reason=reason,
                )
            updated = connection.execute(
                """
                UPDATE repair_confirmations
                SET consumed_at=?, consumed_job_id=?
                WHERE confirmation_id=? AND reserved_job_id=?
                  AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (consumed_at.isoformat(), job_id, confirmation_id, job_id),
            )
            if updated.rowcount != 1:
                raise RepairConfirmationRejectedError(
                    confirmation_id=confirmation_id,
                    reason="concurrent_consumption",
                )
        return self.get(confirmation_id)

    @staticmethod
    def _rejection_reason(
        record: RepairConfirmationRecord,
        *,
        actor_id: str,
        preview_id: str,
        at: datetime,
    ) -> str | None:
        if record.actor_id != actor_id:
            return "actor_mismatch"
        if record.preview_id != preview_id:
            return "preview_mismatch"
        if record.revoked_at is not None:
            return "revoked"
        if record.consumed_at is not None:
            return "already_consumed"
        if record.expires_at <= at:
            return "expired"
        return None

    @staticmethod
    def _record(row: sqlite3.Row) -> RepairConfirmationRecord:
        mutations = json.loads(str(row["acknowledged_mutations_json"]))
        if not isinstance(mutations, list) or any(not isinstance(item, str) for item in mutations):
            raise ValueError("repair confirmation mutations are corrupt")
        action_value = str(row["action"])
        if action_value not in _REPAIR_ACTION_VALUES:
            raise ValueError("repair confirmation action is corrupt")
        selected = row["selected_data_type"]
        return RepairConfirmationRecord(
            confirmation_id=str(row["confirmation_id"]),
            dataset_id=str(row["dataset_id"]),
            version=int(row["version"]),
            preview_id=str(row["preview_id"]),
            action=cast(RepairAction, action_value),
            selected_data_type=DataType(str(selected)) if selected is not None else None,
            acknowledged_mutations=tuple(str(item) for item in mutations),
            actor_id=str(row["actor_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            consumed_at=(
                datetime.fromisoformat(str(row["consumed_at"]))
                if row["consumed_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
            reserved_job_id=(
                str(row["reserved_job_id"]) if row["reserved_job_id"] is not None else None
            ),
            reserved_at=(
                datetime.fromisoformat(str(row["reserved_at"]))
                if row["reserved_at"] is not None
                else None
            ),
            consumed_job_id=(
                str(row["consumed_job_id"]) if row["consumed_job_id"] is not None else None
            ),
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("confirmation timestamps must be timezone-aware")
        return value.astimezone(UTC)
