"""Base SQLite lifecycle for the Strategy Governance ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Self

from .canonical import canonical_sha256
from .errors import EvidenceTamperedError
from .governance_common import _sha


class GovernanceLedgerBase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS governance_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO governance_metadata(key, value)
            VALUES ('schema_version', '1');

            CREATE TABLE IF NOT EXISTS composition_candidates (
                candidate_id TEXT PRIMARY KEY,
                composition_hash TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                complete INTEGER NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_json BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS governance_evidence (
                evidence_digest TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                source_commit_sha TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES composition_candidates(candidate_id)
            );

            CREATE TABLE IF NOT EXISTS governance_approvals (
                approval_digest TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                payload_json BLOB NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES composition_candidates(candidate_id),
                FOREIGN KEY(evidence_digest) REFERENCES governance_evidence(evidence_digest)
            );

            CREATE TABLE IF NOT EXISTS audit_checkpoints (
                session_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                completed_steps_json BLOB NOT NULL,
                checkpoint_digest TEXT NOT NULL,
                FOREIGN KEY(candidate_id) REFERENCES composition_candidates(candidate_id),
                FOREIGN KEY(evidence_digest) REFERENCES governance_evidence(evidence_digest)
            );

            CREATE TABLE IF NOT EXISTS governance_chain (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_kind TEXT NOT NULL,
                record_identity TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                previous_record_hash TEXT,
                record_hash TEXT NOT NULL
            );
            """
        )
        metadata = self._connection.execute(
            "SELECT key, value FROM governance_metadata ORDER BY key"
        ).fetchall()
        if [(row["key"], row["value"]) for row in metadata] != [("schema_version", "1")]:
            raise EvidenceTamperedError("unexpected governance metadata")

    def artifact_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in (
                self.path,
                Path(f"{self.path}-wal"),
                Path(f"{self.path}-shm"),
            )
            if path.exists()
        )

    def _append_chain(self, record_kind: str, record_identity: str, payload_digest: str) -> None:
        previous_row = self._connection.execute(
            "SELECT record_hash FROM governance_chain ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous = None if previous_row is None else str(previous_row["record_hash"])
        record_hash = canonical_sha256(
            {
                "record_kind": record_kind,
                "record_identity": record_identity,
                "payload_digest": _sha(payload_digest, "chain payload_digest"),
                "previous_record_hash": previous,
            }
        )
        self._connection.execute(
            """
            INSERT INTO governance_chain(
                record_kind, record_identity, payload_digest,
                previous_record_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (record_kind, record_identity, payload_digest, previous, record_hash),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = ["GovernanceLedgerBase"]
