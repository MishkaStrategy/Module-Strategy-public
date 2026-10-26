"""Durable idempotency and conflicting-content rejection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import IdempotencyConflictError


class IdempotencyLedger:
    """Map operation IDs to immutable content fingerprints."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                scope TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                result_reference TEXT,
                PRIMARY KEY (scope, operation_id)
            )
            """
        )

    def claim(self, *, scope: str, operation_id: str, content_hash: str) -> bool:
        """Return True for a new claim, False for an exact replay."""

        with self._connection:
            row = self._connection.execute(
                "SELECT content_hash FROM idempotency WHERE scope = ? AND operation_id = ?",
                (scope, operation_id),
            ).fetchone()
            if row is not None:
                if row[0] != content_hash:
                    raise IdempotencyConflictError(
                        f"{scope}:{operation_id} already exists with different content"
                    )
                return False
            self._connection.execute(
                "INSERT INTO idempotency(scope, operation_id, content_hash) VALUES (?, ?, ?)",
                (scope, operation_id, content_hash),
            )
            return True

    def bind_result(self, *, scope: str, operation_id: str, result_reference: str) -> None:
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE idempotency SET result_reference = ? WHERE scope = ? AND operation_id = ?",
                (result_reference, scope, operation_id),
            )
            if cursor.rowcount != 1:
                raise IdempotencyConflictError("cannot bind result for an unclaimed operation")

    def result(self, *, scope: str, operation_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT result_reference FROM idempotency WHERE scope = ? AND operation_id = ?",
            (scope, operation_id),
        ).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        self._connection.close()
