"""Opt-in Core-owned SQLite lifecycle/effect transaction reference.

This store is a local implementation slice, not a production Paper composition.
It owns one immutable instance/broker aggregate and commits broker snapshots,
Core events, lifecycle fences, idempotency records, and receipts in one SQLite
transaction. The host must supply already trusted identity and authority
bindings. No current production runtime wires this store.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audit import Journal
from .broker_state import (
    BrokerStateSnapshot,
    _ClosedFenceStateIncompatible,
    _capture_core_closed_fence_broker_state,
    _capture_quarantine_broker_state,
    _decode_engine_config,
    _decode_instrument,
    capture_broker_state,
    restore_broker_state,
    _restore_core_closed_fence_broker_state,
    _restore_quarantine_broker_state,
)
from .events import EventEnvelope, InMemoryEventStore
from .execution import BrokerSimulator
from .lifecycle import (
    CoreAtomicCommitRejected,
    CoreAuthorityReference,
    CoreEffectCommitReceipt,
    CoreEffectLookupEvidence,
    CoreEffectLookupStatus,
    CoreFenceTransition,
    CoreFenceTransitionReceipt,
    CoreLifecycleAction,
    CoreLifecycleCommitPort,
    CoreLifecycleExpectedGeneration,
    CoreLifecycleEffect,
    CoreSavedCommitReceipt,
    CoreStagedEffectCommit,
    CoreStagedTransitionCommit,
    CoreWriterIdentity,
)
from .models import (
    Candle,
    CancelRequest,
    ClosePositionRequest,
    OrderRequest,
    OrderType,
    PositionSide,
    PositionUpdateRequest,
    Side,
    TimeInForce,
)
from .serialization import reject_sensitive_keys, strict_json_loads, to_jsonable

_SCHEMA_VERSION = 1
_TABLES = {
    "core_lifecycle_schema",
    "core_lifecycle_instance",
    "core_lifecycle_authorities",
    "core_lifecycle_requests",
    "core_lifecycle_events",
}
_GENESIS_EVENT_DIGEST = "0" * 64


@dataclass(frozen=True, slots=True)
class CoreLifecycleStoreSnapshot:
    """Immutable read projection of one committed Core aggregate."""

    lifecycle: CoreLifecycleExpectedGeneration
    writer: CoreWriterIdentity
    risk_enabled: bool
    commit_sequence: int
    broker_state: BrokerStateSnapshot
    events: tuple[EventEnvelope, ...]
    saved_receipts: tuple[CoreSavedCommitReceipt, ...]


class SQLiteCoreLifecycleCommitPort(CoreLifecycleCommitPort):
    """SQLite implementation of the canonical Core lifecycle transaction port.

    The first slice intentionally owns one immutable instance and one
    ``BrokerSimulator`` aggregate. ``trusted_authority_refs`` are injected by a
    trusted host at bootstrap and are pinned for this local store; this module
    does not authenticate their source, refresh them, or implement RESUME.
    With no supplied authority refs, all effect and transition commands fail
    closed. Production runtime wiring remains absent until trusted owner sources
    and every Paper mutation path are integrated.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        binding: CoreLifecycleExpectedGeneration,
        writer: CoreWriterIdentity,
        initial_broker: BrokerSimulator,
        trusted_authority_refs: tuple[CoreAuthorityReference, ...] = (),
        initial_risk_enabled: bool = False,
        clock: Any | None = None,
    ) -> None:
        if not isinstance(binding, CoreLifecycleExpectedGeneration):
            raise TypeError("binding must be CoreLifecycleExpectedGeneration")
        if not isinstance(writer, CoreWriterIdentity):
            raise TypeError("writer must be CoreWriterIdentity")
        if not isinstance(initial_broker, BrokerSimulator):
            raise TypeError("initial_broker must be BrokerSimulator")
        if type(initial_risk_enabled) is not bool:
            raise TypeError("initial_risk_enabled must be bool")
        if initial_risk_enabled and not trusted_authority_refs:
            raise ValueError("an initially open risk fence requires trusted authority references")
        if not isinstance(trusted_authority_refs, tuple) or any(
            not isinstance(item, CoreAuthorityReference) for item in trusted_authority_refs
        ):
            raise TypeError("trusted_authority_refs must be a tuple of CoreAuthorityReference")
        if len(set(trusted_authority_refs)) != len(trusted_authority_refs):
            raise ValueError("trusted_authority_refs must be unique")

        requested_path = Path(path).expanduser()
        if requested_path.is_symlink():
            raise ValueError("Core lifecycle database cannot be a symlink")
        requested_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = requested_path.resolve()
        self.binding = binding
        self.writer = writer
        self._authority_refs = frozenset(trusted_authority_refs)
        self._lock = threading.RLock()
        self._closed = False
        self._clock = clock or (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        self._engine_config = initial_broker.config
        self._instrument = initial_broker.instrument
        initial_snapshot = capture_broker_state(initial_broker)
        if initial_broker.journal.events or initial_snapshot.orders or initial_snapshot.position:
            raise ValueError("new Core lifecycle scope must start with a pristine broker")
        if initial_snapshot.pending_close is not None:
            raise ValueError("new Core lifecycle scope cannot start with a pending close")
        self._initialize(initial_snapshot, initial_risk_enabled)

    def begin_lifecycle_transaction(self) -> _SQLiteCoreLifecycleTransaction:
        self._lock.acquire()
        try:
            if self._closed:
                raise RuntimeError("Core lifecycle store is closed")
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            return _SQLiteCoreLifecycleTransaction(self, connection)
        except BaseException:
            self._lock.release()
            raise

    def snapshot(self) -> CoreLifecycleStoreSnapshot:
        with self._lock:
            if self._closed:
                raise RuntimeError("Core lifecycle store is closed")
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                row = self._read_instance(connection)
                self._check_row_binding(row)
                self._check_authority_inventory(connection)
                events = self._read_events(connection)
                _validate_request_receipts(connection, int(row["commit_sequence"]), events)
                saved_receipts = _read_saved_receipts(connection)
                state = _decode_state(row["broker_state_json"], row["broker_state_sha256"])
                self._validate_config(state)
                quarantine = _requires_quarantine(
                    bool(row["risk_enabled"]), state, events, self.binding
                )
                _restore_private_broker(
                    state,
                    events,
                    self.binding,
                    risk_enabled=bool(row["risk_enabled"]),
                    quarantine=quarantine,
                )
                generation = int(row["generation"])
                snapshot = CoreLifecycleStoreSnapshot(
                    lifecycle=CoreLifecycleExpectedGeneration(
                        self.binding.instance_id,
                        self.binding.incarnation_id,
                        self.binding.package_id,
                        self.binding.package_digest,
                        generation,
                    ),
                    writer=self.writer,
                    risk_enabled=bool(row["risk_enabled"]),
                    commit_sequence=int(row["commit_sequence"]),
                    broker_state=state,
                    events=events,
                    saved_receipts=saved_receipts,
                )
                connection.execute("COMMIT")
                return snapshot
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def lookup_effect(self, request: CoreLifecycleEffect) -> CoreEffectLookupEvidence:
        """Return read-only evidence from one fully validated Core snapshot.

        Historical matches are not compared with the current lifecycle generation:
        a later fence may advance that generation without invalidating an earlier
        committed effect. Absence is bounded by the snapshot's commit and event
        sequences and is not a promise about later writes.
        """
        if type(request) is not CoreLifecycleEffect:
            raise TypeError("request must be CoreLifecycleEffect")
        request.__post_init__()
        try:
            snapshot = self.snapshot()
        except Exception as exc:
            return CoreEffectLookupEvidence(
                CoreEffectLookupStatus.UNAVAILABLE,
                request.effect_id,
                request.request_digest,
                None,
                None,
                reason=f"validated Core snapshot unavailable ({type(exc).__name__})",
            )
        matches = tuple(
            item for item in snapshot.saved_receipts if item.request_id == request.effect_id
        )
        observed_event_sequence = snapshot.events[-1].sequence if snapshot.events else 0
        request_scope = (
            request.lifecycle.instance_id,
            request.lifecycle.incarnation_id,
            request.lifecycle.package_id,
            request.lifecycle.package_digest,
        )
        snapshot_scope = (
            snapshot.lifecycle.instance_id,
            snapshot.lifecycle.incarnation_id,
            snapshot.lifecycle.package_id,
            snapshot.lifecycle.package_digest,
        )
        if request_scope != snapshot_scope:
            return CoreEffectLookupEvidence(
                CoreEffectLookupStatus.CONFLICT,
                request.effect_id,
                request.request_digest,
                snapshot.commit_sequence,
                observed_event_sequence,
                reason="effect request belongs to another Core instance or package scope",
            )
        if not matches:
            return CoreEffectLookupEvidence(
                CoreEffectLookupStatus.ABSENT_AT_READ,
                request.effect_id,
                request.request_digest,
                snapshot.commit_sequence,
                observed_event_sequence,
                reason="effect ID absent from validated snapshot",
            )
        if len(matches) != 1:
            status = CoreEffectLookupStatus.CONFLICT
            reason = "effect ID has multiple saved receipts"
        else:
            saved = matches[0]
            receipt = saved.receipt
            if (
                saved.kind != "effect"
                or saved.request_digest != request.request_digest
                or type(receipt) is not CoreEffectCommitReceipt
                or not receipt.matches_request(request)
            ):
                status = CoreEffectLookupStatus.CONFLICT
                reason = "saved Core receipt does not match complete effect request"
            else:
                return CoreEffectLookupEvidence(
                    CoreEffectLookupStatus.MATCHED,
                    request.effect_id,
                    request.request_digest,
                    snapshot.commit_sequence,
                    observed_event_sequence,
                    receipt=receipt,
                    reason="validated historical Core effect receipt matched",
                )
        return CoreEffectLookupEvidence(
            status,
            request.effect_id,
            request.request_digest,
            snapshot.commit_sequence,
            observed_event_sequence,
            reason=reason,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self, initial_snapshot: BrokerStateSnapshot, risk_enabled: bool) -> None:
        connection = self._connect()
        try:
            tables = _user_tables(connection)
            if not tables:
                if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ValueError("Core lifecycle database contains an unknown schema")
                connection.execute("PRAGMA journal_mode=WAL")
                # executescript() implicitly commits an already-open transaction.
                # Start the transaction inside the script so schema creation and
                # the first aggregate are committed (or rolled back) together.
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_SQL)
                try:
                    initial_state_json = _canonical_json(initial_snapshot.to_state())
                    connection.execute(
                        "INSERT INTO core_lifecycle_schema(version) VALUES(?)",
                        (_SCHEMA_VERSION,),
                    )
                    connection.execute(
                        "INSERT INTO core_lifecycle_instance VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            self.binding.instance_id,
                            self.binding.incarnation_id,
                            self.binding.package_id,
                            self.binding.package_digest,
                            self.writer.owner_id,
                            self.writer.epoch,
                            self.binding.expected_generation,
                            int(risk_enabled),
                            0,
                            initial_state_json,
                            _sha256(initial_state_json.encode()),
                            _aware_now(self._clock).isoformat(),
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO core_lifecycle_authorities VALUES(?,?,?,1)",
                        [
                            (item.authority, item.reference_id, item.digest)
                            for item in sorted(
                                self._authority_refs,
                                key=lambda value: (value.authority, value.reference_id),
                            )
                        ],
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
            self._validate_database(connection)
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()

    def _validate_database(self, connection: sqlite3.Connection) -> None:
        if _user_tables(connection) != _TABLES:
            raise ValueError("Core lifecycle database schema is unrelated or incomplete")
        _validate_table_columns(connection)
        versions = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM core_lifecycle_schema ORDER BY version"
            )
        )
        if versions != (_SCHEMA_VERSION,):
            raise ValueError("unsupported Core lifecycle database schema")
        row = self._read_instance(connection)
        self._check_row_binding(row)
        self._check_authority_inventory(connection)
        state = _decode_state(row["broker_state_json"], row["broker_state_sha256"])
        self._validate_config(state)
        events = self._read_events(connection)
        _validate_request_receipts(connection, int(row["commit_sequence"]), events)
        quarantine = _requires_quarantine(bool(row["risk_enabled"]), state, events, self.binding)
        _restore_private_broker(
            state,
            events,
            self.binding,
            risk_enabled=bool(row["risk_enabled"]),
            quarantine=quarantine,
        )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Core lifecycle database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Core lifecycle database foreign key check failed")

    def _check_authority_inventory(self, connection: sqlite3.Connection) -> None:
        authorities = frozenset(
            CoreAuthorityReference(str(item[0]), str(item[1]), str(item[2]))
            for item in connection.execute(
                "SELECT authority,reference_id,digest FROM core_lifecycle_authorities "
                "WHERE current=1"
            )
        )
        if authorities != self._authority_refs:
            raise CoreAtomicCommitRejected("Core authority snapshot changed")

    def _validate_config(self, snapshot: BrokerStateSnapshot) -> None:
        if _decode_engine_config(snapshot.config) != self._engine_config:
            raise ValueError("Core lifecycle broker configuration identity mismatch")
        if _decode_instrument(snapshot.instrument) != self._instrument:
            raise ValueError("Core lifecycle broker instrument identity mismatch")

    def _read_instance(self, connection: sqlite3.Connection) -> sqlite3.Row:
        rows = connection.execute("SELECT * FROM core_lifecycle_instance").fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise ValueError("Core lifecycle database must contain exactly one aggregate")
        return rows[0]

    def _check_row_binding(self, row: sqlite3.Row) -> None:
        expected = (
            self.binding.instance_id,
            self.binding.incarnation_id,
            self.binding.package_id,
            self.binding.package_digest,
            self.writer.owner_id,
            self.writer.epoch,
        )
        actual = tuple(
            row[name]
            for name in (
                "instance_id",
                "incarnation_id",
                "package_id",
                "package_digest",
                "writer_owner_id",
                "writer_epoch",
            )
        )
        if actual != expected:
            raise CoreAtomicCommitRejected("Core instance binding or writer epoch changed")

    def _read_events(self, connection: sqlite3.Connection) -> tuple[EventEnvelope, ...]:
        raw_rows = connection.execute(
            "SELECT sequence,event_json,event_sha256,previous_sha256 "
            "FROM core_lifecycle_events ORDER BY sequence"
        ).fetchall()
        events: list[EventEnvelope] = []
        previous = _GENESIS_EVENT_DIGEST
        previous_timestamp: datetime | None = None
        for expected_sequence, row in enumerate(raw_rows, start=1):
            encoded = str(row["event_json"])
            digest = _sha256(encoded.encode())
            if (
                int(row["sequence"]) != expected_sequence
                or row["event_sha256"] != digest
                or row["previous_sha256"] != previous
            ):
                raise ValueError("Core lifecycle event chain is inconsistent")
            event = _event_from_json(encoded)
            if (
                event.sequence != expected_sequence
                or event.run_id != _run_id(self.binding)
                or _canonical_json(to_jsonable(event)) != encoded
                or (previous_timestamp is not None and event.timestamp < previous_timestamp)
            ):
                raise ValueError("Core lifecycle event scope or sequence is invalid")
            events.append(event)
            previous = digest
            previous_timestamp = event.timestamp
        store = InMemoryEventStore()
        store.append_many(tuple(events))
        return tuple(events)


class _SQLiteCoreLifecycleTransaction:
    def __init__(
        self, store: SQLiteCoreLifecycleCommitPort, connection: sqlite3.Connection
    ) -> None:
        self._store = store
        self._connection = connection
        self._active = True
        self._staged = False

    def check_and_stage_effect(self, request: CoreLifecycleEffect) -> CoreStagedEffectCommit:
        self._require_active()
        if not isinstance(request, CoreLifecycleEffect):
            raise TypeError("request must be CoreLifecycleEffect")
        self._check_binding(request.lifecycle, request.writer)
        row = self._store._read_instance(self._connection)
        self._store._check_row_binding(row)
        self._store._check_authority_inventory(self._connection)
        events = self._store._read_events(self._connection)
        _validate_request_receipts(self._connection, int(row["commit_sequence"]), events)
        request_digest = request.request_digest
        replay = self._read_replay(request.effect_id, "effect", request_digest)
        if replay is not None:
            receipt = _effect_receipt_from_json(replay)
            if not receipt.matches_request(request):
                raise CoreAtomicCommitRejected("stored effect receipt does not match request")
            return CoreStagedEffectCommit(receipt, replayed=True)

        self._check_generation(request.lifecycle.expected_generation, row)
        self._check_authorities(request.authority_refs)
        risk_enabled = bool(row["risk_enabled"])
        payload = _effect_payload(request)
        current_state = _decode_state(row["broker_state_json"], row["broker_state_sha256"])
        self._store._validate_config(current_state)
        quarantine = _requires_quarantine(
            risk_enabled, current_state, events, self._store.binding
        )
        staged_broker, event_store = _restore_private_broker(
            current_state,
            events,
            self._store.binding,
            risk_enabled=risk_enabled,
            quarantine=quarantine,
        )
        before = (
            capture_broker_state(staged_broker)
            if risk_enabled
            else (
                _capture_quarantine_broker_state(staged_broker)
                if quarantine
                else _capture_core_closed_fence_broker_state(staged_broker)
            )
        )
        _apply_effect(staged_broker, request, payload, risk_enabled)
        after = (
            capture_broker_state(staged_broker)
            if risk_enabled
            else (
                _capture_quarantine_broker_state(staged_broker)
                if quarantine
                else _capture_core_closed_fence_broker_state(staged_broker)
            )
        )
        if not risk_enabled and _effect_increases_risk(before, after):
            raise CoreAtomicCommitRejected("Core risk fence is closed for this effect")
        broker_events = event_store.read(after_sequence=len(events))
        _validate_event_time_order(events, broker_events)
        commit_sequence = int(row["commit_sequence"]) + 1
        committed_at = _aware_now(self._store._clock)
        state_validation_mode = "quarantine" if not risk_enabled and quarantine else "default"
        event_timestamp = (
            broker_events[-1].timestamp if broker_events else _effect_event_timestamp(payload)
        )
        if events and event_timestamp < events[-1].timestamp:
            raise CoreAtomicCommitRejected("Core effect event time precedes committed history")
        staged_broker.journal.emit_event(
            timestamp=event_timestamp,
            event_type="core.effect.committed",
            source="modular_core.lifecycle_sqlite",
            payload={
                "instance_id": self._store.binding.instance_id,
                "request_id": request.effect_id,
                "request_digest": request_digest,
                "canonical_commit_sequence": commit_sequence,
                "broker_event_start": broker_events[0].sequence if broker_events else None,
                "broker_event_end": broker_events[-1].sequence if broker_events else None,
                "broker_event_count": len(broker_events),
                "state_validation_mode": state_validation_mode,
            },
            correlation_id=request.effect_id,
            entity_key=f"instance:{self._store.binding.instance_id}",
        )
        new_events = event_store.read(after_sequence=len(events))
        receipt = CoreEffectCommitReceipt(
            request.effect_id,
            self._store.binding.instance_id,
            self._store.binding.incarnation_id,
            self._store.binding.package_id,
            self._store.binding.package_digest,
            self._store.writer,
            request.lifecycle.expected_generation,
            request.canonical_payload_digest,
            request.operation,
            request.target_ids,
            request.authority_refs,
            commit_sequence,
            committed_at,
        )
        self._save_broker_effect(
            row,
            request.effect_id,
            "effect",
            request_digest,
            receipt,
            after,
            new_events,
            commit_sequence,
            committed_at,
        )
        self._staged = True
        return CoreStagedEffectCommit(receipt, replayed=False)

    def check_and_stage_transition(
        self, request: CoreFenceTransition
    ) -> CoreStagedTransitionCommit:
        self._require_active()
        if not isinstance(request, CoreFenceTransition):
            raise TypeError("request must be CoreFenceTransition")
        self._check_binding(request.lifecycle, request.writer)
        row = self._store._read_instance(self._connection)
        self._store._check_row_binding(row)
        self._store._check_authority_inventory(self._connection)
        events = self._store._read_events(self._connection)
        _validate_request_receipts(self._connection, int(row["commit_sequence"]), events)
        request_digest = request.request_digest
        replay = self._read_replay(request.transition_id, "transition", request_digest)
        if replay is not None:
            receipt = _transition_receipt_from_json(replay)
            if not receipt.matches_request(request):
                raise CoreAtomicCommitRejected("stored transition receipt does not match request")
            return CoreStagedTransitionCommit(receipt, replayed=True)

        self._check_generation(request.lifecycle.expected_generation, row)
        self._check_authorities(request.authority_refs)
        _validate_transition_payload(request)
        if request.action is not CoreLifecycleAction.SUSPEND:
            raise CoreAtomicCommitRejected("Core RESUME authority source unavailable")
        if not bool(row["risk_enabled"]):
            raise CoreAtomicCommitRejected("Core risk fence is already closed")
        state = _decode_state(row["broker_state_json"], row["broker_state_sha256"])
        self._store._validate_config(state)
        staged_broker, _ = _restore_private_broker(
            state,
            events,
            self._store.binding,
            risk_enabled=True,
        )
        try:
            _capture_core_closed_fence_broker_state(staged_broker)
            reconciliation_required = False
        except _ClosedFenceStateIncompatible:
            # State already passed DEFAULT validation. Preserve it as quarantine
            # evidence instead of rejecting the irreversible risk-off transition.
            reconciliation_required = True
        commit_sequence = int(row["commit_sequence"]) + 1
        committed_at = _aware_now(self._store._clock)
        receipt = CoreFenceTransitionReceipt(
            request.transition_id,
            self._store.binding.instance_id,
            self._store.binding.incarnation_id,
            self._store.binding.package_id,
            self._store.binding.package_digest,
            self._store.writer,
            request.lifecycle.expected_generation,
            request.resulting_generation,
            request.action,
            request.canonical_payload_digest,
            request.authority_refs,
            commit_sequence,
            committed_at,
        )
        event = self._lifecycle_event(
            sequence=len(events) + 1,
            timestamp=events[-1].timestamp if events else committed_at,
            event_type="core.lifecycle.suspended",
            request_id=request.transition_id,
            request_digest=request_digest,
            generation=request.resulting_generation,
            commit_sequence=commit_sequence,
            reconciliation_required=reconciliation_required,
        )
        updated_state = str(row["broker_state_json"])
        updated_state_digest = str(row["broker_state_sha256"])
        self._connection.execute(
            "UPDATE core_lifecycle_instance SET generation=?,risk_enabled=0,"
            "commit_sequence=?,updated_at=? WHERE singleton=1",
            (request.resulting_generation, commit_sequence, committed_at.isoformat()),
        )
        self._append_events((event,))
        self._insert_request(
            request.transition_id,
            "transition",
            request_digest,
            _transition_receipt_json(receipt),
            commit_sequence,
        )
        self._staged = True
        if _sha256(updated_state.encode()) != updated_state_digest:
            raise ValueError("Core broker snapshot changed during fence transition")
        return CoreStagedTransitionCommit(receipt, replayed=False)

    def commit(self) -> None:
        self._require_active()
        try:
            self._connection.execute("COMMIT")
        finally:
            self._finish()

    def rollback(self) -> None:
        if not self._active:
            return
        try:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        finally:
            self._finish()

    def _save_broker_effect(
        self,
        row: sqlite3.Row,
        request_id: str,
        kind: str,
        request_digest: str,
        receipt: CoreEffectCommitReceipt,
        state: BrokerStateSnapshot,
        events: Sequence[EventEnvelope],
        commit_sequence: int,
        committed_at: datetime,
    ) -> None:
        encoded_state = _canonical_json(state.to_state())
        self._connection.execute(
            "UPDATE core_lifecycle_instance SET broker_state_json=?,broker_state_sha256=?,"
            "commit_sequence=?,updated_at=? WHERE singleton=1",
            (
                encoded_state,
                _sha256(encoded_state.encode()),
                commit_sequence,
                committed_at.isoformat(),
            ),
        )
        self._append_events(events)
        self._insert_request(
            request_id,
            kind,
            request_digest,
            _effect_receipt_json(receipt),
            commit_sequence,
        )

    def _append_events(self, events: Sequence[EventEnvelope]) -> None:
        if not events:
            return
        previous = self._connection.execute(
            "SELECT event_sha256 FROM core_lifecycle_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_digest = _GENESIS_EVENT_DIGEST if previous is None else str(previous[0])
        expected = self._next_event_sequence()
        for event in events:
            if event.sequence != expected:
                raise ValueError("Core event sequence is not contiguous")
            encoded = _canonical_json(to_jsonable(event))
            digest = _sha256(encoded.encode())
            self._connection.execute(
                "INSERT INTO core_lifecycle_events VALUES(?,?,?,?)",
                (expected, encoded, digest, previous_digest),
            )
            previous_digest = digest
            expected += 1

    def _insert_request(
        self, request_id: str, kind: str, request_digest: str, receipt_json: str, sequence: int
    ) -> None:
        self._connection.execute(
            "INSERT INTO core_lifecycle_requests VALUES(?,?,?,?,?,?)",
            (
                request_id,
                kind,
                request_digest,
                receipt_json,
                _sha256(receipt_json.encode()),
                sequence,
            ),
        )

    def _lifecycle_event(
        self,
        *,
        sequence: int,
        timestamp: datetime,
        event_type: str,
        request_id: str,
        request_digest: str,
        generation: int,
        commit_sequence: int,
        reconciliation_required: bool = False,
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=f"core-evt-{sequence:010d}",
            run_id=_run_id(self._store.binding),
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            source="modular_core.lifecycle_sqlite",
            payload={
                "instance_id": self._store.binding.instance_id,
                "request_id": request_id,
                "request_digest": request_digest,
                "generation": generation,
                "canonical_commit_sequence": commit_sequence,
                "reconciliation_required": reconciliation_required,
            },
            correlation_id=request_id,
            entity_key=f"instance:{self._store.binding.instance_id}",
        )

    def _next_event_sequence(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM core_lifecycle_events"
        ).fetchone()
        return int(row[0])

    def _read_replay(self, request_id: str, kind: str, request_digest: str) -> str | None:
        row = self._connection.execute(
            "SELECT request_kind,request_digest,receipt_json,receipt_sha256,commit_sequence "
            "FROM core_lifecycle_requests "
            "WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_kind"] != kind or row["request_digest"] != request_digest:
            raise CoreAtomicCommitRejected("idempotency key was already used by another request")
        raw = str(row["receipt_json"])
        if _sha256(raw.encode()) != row["receipt_sha256"]:
            raise CoreAtomicCommitRejected("stored Core receipt digest mismatch")
        receipt = (
            _effect_receipt_from_json(raw)
            if kind == "effect"
            else _transition_receipt_from_json(raw)
        )
        if (
            _canonical_json(strict_json_loads(raw)) != raw
            or receipt.canonical_commit_sequence != int(row["commit_sequence"])
            or receipt.canonical_commit_sequence
            > int(self._store._read_instance(self._connection)["commit_sequence"])
        ):
            raise CoreAtomicCommitRejected("stored Core receipt sequence or encoding is invalid")
        return raw

    def _check_binding(
        self, lifecycle: CoreLifecycleExpectedGeneration, writer: CoreWriterIdentity
    ) -> None:
        if writer != self._store.writer:
            raise CoreAtomicCommitRejected("Core writer epoch mismatch")
        actual = (
            lifecycle.instance_id,
            lifecycle.incarnation_id,
            lifecycle.package_id,
            lifecycle.package_digest,
        )
        expected = (
            self._store.binding.instance_id,
            self._store.binding.incarnation_id,
            self._store.binding.package_id,
            self._store.binding.package_digest,
        )
        if actual != expected:
            raise CoreAtomicCommitRejected("Core instance or package binding mismatch")

    def _check_generation(self, expected: int, row: sqlite3.Row) -> None:
        current = int(row["generation"])
        if current != expected:
            raise CoreAtomicCommitRejected(
                f"stale Core lifecycle generation: expected {expected}, current {current}"
            )

    def _check_authorities(self, authorities: tuple[CoreAuthorityReference, ...]) -> None:
        if not authorities:
            raise CoreAtomicCommitRejected("Core authority source unavailable")
        rows = {
            CoreAuthorityReference(str(row[0]), str(row[1]), str(row[2]))
            for row in self._connection.execute(
                "SELECT authority,reference_id,digest FROM core_lifecycle_authorities "
                "WHERE current=1"
            )
        }
        if rows != self._store._authority_refs:
            raise CoreAtomicCommitRejected("Core authority binding changed or is unavailable")
        if len(set(authorities)) != len(authorities) or not set(authorities) <= rows:
            raise CoreAtomicCommitRejected("Core authority binding unavailable or stale")

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("Core lifecycle transaction is closed")

    def _finish(self) -> None:
        if self._active:
            self._active = False
            try:
                self._connection.close()
            finally:
                self._store._lock.release()


def _apply_effect(
    broker: BrokerSimulator,
    request: CoreLifecycleEffect,
    payload: dict[str, Any],
    risk_enabled: bool,
) -> None:
    operation = payload["operation"]
    if operation != request.operation:
        raise CoreAtomicCommitRejected("Core operation differs from canonical payload")
    _validate_targets(request, payload)
    if broker._core_quarantine_validation:
        if operation != "process_bar":
            raise CoreAtomicCommitRejected(
                "Core reconciliation quarantine blocks broker effects"
            )
        bar = _candle(_mapping(payload["bar"], "bar"))
        if bar.symbol != broker.instrument.symbol:
            raise CoreAtomicCommitRejected("Core bar symbol does not match broker instrument")
        try:
            broker._process_quarantine_position_exit(
                bar,
                _integer(payload["index"], "index"),
            )
        except ValueError as exc:
            raise CoreAtomicCommitRejected(str(exc)) from exc
        return
    if operation == "submit_order":
        order_data = _mapping(payload["request"], "order request")
        order = _order_request(order_data)
        if not risk_enabled and not (order.reduce_only or order.close_position):
            raise CoreAtomicCommitRejected("Core risk fence is closed for new-risk order")
        result = broker.submit(
            order,
            source_module=_text(payload["source_module"], "source_module"),
            created_at=_timestamp(payload["created_at"], "created_at"),
            current_index=_integer(payload["current_index"], "current_index"),
            mark_price=_number(payload["mark_price"], "mark_price"),
        )
        if result is None:
            raise CoreAtomicCommitRejected("Core broker rejected the order")
        return
    if operation == "cancel_order":
        request_data = _mapping(payload["request"], "cancel request")
        cancel_request = CancelRequest(**request_data)
        matches = [
            order for order in broker.pending_orders if _matches_cancel(order, cancel_request)
        ]
        if len(matches) != 1:
            raise CoreAtomicCommitRejected("Core cancel target is not one exact pending order")
        if not risk_enabled and _is_protective_order(matches[0]):
            raise CoreAtomicCommitRejected("Core risk fence blocks removal of protection")
        count = broker.cancel(
            cancel_request,
            timestamp=_timestamp(payload["timestamp"], "timestamp"),
            source_module=_text(payload["source_module"], "source_module"),
        )
        if count != 1:
            raise CoreAtomicCommitRejected("Core broker did not cancel the exact order")
        return
    if operation == "update_position":
        update = PositionUpdateRequest(**_mapping(payload["request"], "position update"))
        before = broker.portfolio.position
        if before is None:
            raise CoreAtomicCommitRejected("Core position does not exist")
        if request.target_ids != (before.id,):
            raise CoreAtomicCommitRejected("Core position target does not match canonical position")
        old_stop, old_target, side = before.stop_loss, before.take_profit, before.side
        changed = broker.update_position(
            update,
            timestamp=_timestamp(payload["timestamp"], "timestamp"),
            source_module=_text(payload["source_module"], "source_module"),
            mark_price=_number(payload["mark_price"], "mark_price"),
        )
        if not changed:
            raise CoreAtomicCommitRejected("Core broker rejected the position update")
        after = broker.portfolio.position
        if after is None or after.side is not side:
            raise CoreAtomicCommitRejected(
                "Core position identity changed during protection update"
            )
        if not risk_enabled and not _protection_is_not_weaker(
            side, old_stop, old_target, after.stop_loss, after.take_profit
        ):
            raise CoreAtomicCommitRejected("Core risk fence blocks a weaker protection update")
        return
    if operation == "schedule_close":
        close = ClosePositionRequest(**_mapping(payload["request"], "close request"))
        position = broker.portfolio.position
        if position is None or request.target_ids != (position.id,):
            raise CoreAtomicCommitRejected("Core close target does not match canonical position")
        if not risk_enabled and any(_is_protective_order(order) for order in broker.pending_orders):
            raise CoreAtomicCommitRejected(
                "Core closed-fence close conflicts with pending reduce-only order"
            )
        if not broker.schedule_close(
            close,
            timestamp=_timestamp(payload["timestamp"], "timestamp"),
            current_index=_integer(payload["current_index"], "current_index"),
            source_module=_text(payload["source_module"], "source_module"),
        ):
            raise CoreAtomicCommitRejected("Core broker rejected the close request")
        return
    if operation == "process_bar":
        bar = _candle(_mapping(payload["bar"], "bar"))
        if bar.symbol != broker.instrument.symbol:
            raise CoreAtomicCommitRejected("Core bar symbol does not match broker instrument")
        index = _integer(payload["index"], "index")
        if risk_enabled:
            broker.process_bar(bar, index)
        else:
            broker._process_bar_with_closed_risk_fence(bar, index)
        return
    if operation == "cancel_all":
        if not risk_enabled and any(_is_protective_order(order) for order in broker.pending_orders):
            raise CoreAtomicCommitRejected("Core risk fence blocks cancel-all of protection")
        broker.cancel_all(
            _timestamp(payload["timestamp"], "timestamp"),
            _text(payload["reason"], "reason"),
        )
        return
    raise CoreAtomicCommitRejected("unsupported Core broker operation")


def _effect_increases_risk(before: BrokerStateSnapshot, after: BrokerStateSnapshot) -> bool:
    previous = before.to_state()
    current = after.to_state()
    previous_position = previous["position"]
    current_position = current["position"]
    position_is_flat = previous_position is not None and current_position is None
    position_reduced = (
        previous_position is not None
        and current_position is not None
        and previous_position["side"] == current_position["side"]
        and float(current_position["quantity"]) < float(previous_position["quantity"])
    )
    if previous_position is None and current_position is not None:
        return True
    if previous_position is not None and current_position is not None:
        if previous_position["side"] != current_position["side"]:
            return True
        if float(current_position["quantity"]) > float(previous_position["quantity"]):
            return True
        old_stop = previous_position.get("stop_loss")
        new_stop = current_position.get("stop_loss")
        old_target = previous_position.get("take_profit")
        new_target = current_position.get("take_profit")
        if not _protection_is_not_weaker(
            PositionSide(previous_position["side"]), old_stop, old_target, new_stop, new_target
        ):
            return True

    old_orders = {str(order["id"]): order for order in previous["orders"]}
    new_orders = {str(order["id"]): order for order in current["orders"]}
    for order_id, order in new_orders.items():
        if order_id not in old_orders and not _mapping_protective(order):
            return True
        old = old_orders.get(order_id)
        if old is not None:
            old_remaining = _order_remaining(old)
            new_remaining = _order_remaining(order)
            if new_remaining > old_remaining and not _mapping_protective(order):
                return True
    for order_id, old in old_orders.items():
        new = new_orders.get(order_id)
        if not _mapping_protective(old) or position_is_flat:
            continue
        if new is None:
            return True
        was_active = old["status"] not in {"filled", "cancelled", "expired", "rejected"}
        if was_active and new["status"] in {"cancelled", "expired", "rejected"}:
            return True
        if was_active and new["status"] == "filled" and not position_reduced:
            return True
    if previous.get("pending_close") is not None and current.get("pending_close") is None:
        if current_position is not None and not position_reduced:
            return True
    return False


def _protection_is_not_weaker(
    side: PositionSide,
    old_stop: float | None,
    old_target: float | None,
    new_stop: float | None,
    new_target: float | None,
) -> bool:
    if old_target != new_target:
        return False
    if old_stop is None:
        return True
    if new_stop is None:
        return False
    return new_stop >= old_stop if side is PositionSide.LONG else new_stop <= old_stop


def _mapping_protective(order: Mapping[str, Any]) -> bool:
    return bool(order.get("reduce_only") or order.get("close_position"))


def _is_protective_order(order: Any) -> bool:
    return bool(order.reduce_only or order.close_position)


def _matches_cancel(order: Any, request: CancelRequest) -> bool:
    if request.order_id is not None:
        return order.id == request.order_id
    if request.client_order_id is not None:
        return order.client_order_id == request.client_order_id
    if request.tag is not None:
        return order.tag == request.tag
    return False


def _order_remaining(order: Mapping[str, Any]) -> float:
    return max(0.0, float(order["quantity"]) - float(order["filled_quantity"]))


def _validate_targets(request: CoreLifecycleEffect, payload: dict[str, Any]) -> None:
    if request.operation == "submit_order":
        client_id = _mapping(payload["request"], "order request").get("client_order_id")
        if not isinstance(client_id, str) or request.target_ids != (client_id,):
            raise CoreAtomicCommitRejected("Core submit must bind one exact client order id")
        return
    if request.operation == "cancel_order":
        cancel = _mapping(payload["request"], "cancel request")
        selected = tuple(
            value
            for name in ("order_id", "client_order_id", "tag")
            if (value := cancel.get(name)) is not None
        )
        if len(selected) != 1 or request.target_ids != selected:
            raise CoreAtomicCommitRejected("Core cancel targets do not match canonical selector")
        if "tag" in cancel and cancel.get("tag") is not None:
            raise CoreAtomicCommitRejected("Core cancel requires an exact order identity")
        return
    if request.operation == "update_position":
        if len(request.target_ids) != 1:
            raise CoreAtomicCommitRejected("Core position update requires one exact position id")
        return
    if request.operation == "schedule_close":
        if len(request.target_ids) != 1:
            raise CoreAtomicCommitRejected("Core close requires one exact position id")
        return
    if request.operation in {"process_bar", "cancel_all"} and request.target_ids:
        raise CoreAtomicCommitRejected("Core operation does not accept explicit targets")


def _effect_payload(request: CoreLifecycleEffect) -> dict[str, Any]:
    value = strict_json_loads(request.canonical_payload)
    if not isinstance(value, dict):
        raise CoreAtomicCommitRejected("Core effect payload must be a JSON object")
    reject_sensitive_keys(value, "Core effect payload")
    if _canonical_json(value).encode() != request.canonical_payload:
        raise CoreAtomicCommitRejected("Core effect payload is not canonical JSON")
    operation = value.get("operation")
    if operation != request.operation:
        raise CoreAtomicCommitRejected("Core operation differs from canonical payload")
    expected_fields = {
        "submit_order": {
            "operation",
            "request",
            "source_module",
            "created_at",
            "current_index",
            "mark_price",
        },
        "cancel_order": {"operation", "request", "source_module", "timestamp"},
        "update_position": {"operation", "request", "source_module", "timestamp", "mark_price"},
        "schedule_close": {"operation", "request", "source_module", "timestamp", "current_index"},
        "process_bar": {"operation", "bar", "index"},
        "cancel_all": {"operation", "timestamp", "reason"},
    }.get(str(operation))
    if expected_fields is None or set(value) != expected_fields:
        raise CoreAtomicCommitRejected("Core effect payload fields are unsupported")
    return value


def _validate_transition_payload(request: CoreFenceTransition) -> None:
    value = strict_json_loads(request.canonical_payload)
    if not isinstance(value, dict) or _canonical_json(value).encode() != request.canonical_payload:
        raise CoreAtomicCommitRejected("Core transition payload is not a canonical JSON object")
    reject_sensitive_keys(value, "Core transition payload")
    if value != {"action": request.action.value}:
        raise CoreAtomicCommitRejected("Core transition payload does not match requested action")


def _effect_event_timestamp(payload: dict[str, Any]) -> datetime:
    operation = payload["operation"]
    if operation == "process_bar":
        timestamp = _mapping(payload["bar"], "bar").get("timestamp")
        return _timestamp(timestamp, "bar timestamp")
    field = "created_at" if operation == "submit_order" else "timestamp"
    return _timestamp(payload[field], field)


def _validate_event_time_order(
    previous: tuple[EventEnvelope, ...], new_events: tuple[EventEnvelope, ...]
) -> None:
    previous_timestamp = previous[-1].timestamp if previous else None
    for event in new_events:
        if previous_timestamp is not None and event.timestamp < previous_timestamp:
            raise CoreAtomicCommitRejected("Core effect events precede committed event history")
        previous_timestamp = event.timestamp


def _order_request(value: dict[str, Any]) -> OrderRequest:
    allowed = {
        "side",
        "order_type",
        "quantity",
        "price",
        "stop_loss",
        "take_profit",
        "risk_fraction",
        "tag",
        "setup_id",
        "client_order_id",
        "ttl_bars",
        "metadata",
        "time_in_force",
        "post_only",
        "reduce_only",
        "good_till_time",
        "close_position",
        "trigger_price",
        "limit_price",
        "position_side",
    }
    if set(value) - allowed:
        raise CoreAtomicCommitRejected("Core order request contains unsupported fields")
    data = dict(value)
    try:
        data["side"] = Side(data["side"])
        data["order_type"] = OrderType(data["order_type"])
        if "time_in_force" in data:
            data["time_in_force"] = TimeInForce(data["time_in_force"])
        if data.get("position_side") is not None:
            data["position_side"] = PositionSide(data["position_side"])
        if data.get("good_till_time") is not None:
            data["good_till_time"] = _timestamp(data["good_till_time"], "good_till_time")
        return OrderRequest(**data)
    except (KeyError, TypeError, ValueError) as exc:
        raise CoreAtomicCommitRejected("Core order request is malformed") from exc


def _candle(value: dict[str, Any]) -> Candle:
    if set(value) != {"timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe"}:
        raise CoreAtomicCommitRejected("Core bar fields are unsupported")
    try:
        return Candle(
            timestamp=_timestamp(value["timestamp"], "bar timestamp"),
            open=_number(value["open"], "bar open"),
            high=_number(value["high"], "bar high"),
            low=_number(value["low"], "bar low"),
            close=_number(value["close"], "bar close"),
            volume=_number(value["volume"], "bar volume"),
            symbol=_text(value["symbol"], "bar symbol"),
            timeframe=_text(value["timeframe"], "bar timeframe"),
        )
    except (TypeError, ValueError) as exc:
        raise CoreAtomicCommitRejected("Core bar is malformed") from exc


def _restore_private_broker(
    snapshot: BrokerStateSnapshot,
    events: tuple[EventEnvelope, ...],
    binding: CoreLifecycleExpectedGeneration,
    *,
    risk_enabled: bool,
    quarantine: bool = False,
) -> tuple[BrokerSimulator, InMemoryEventStore]:
    config = _decode_engine_config(snapshot.config)
    instrument = _decode_instrument(snapshot.instrument)
    event_store = InMemoryEventStore()
    event_store.append_many(events)
    broker = BrokerSimulator(
        config, Journal(run_id=_run_id(binding), event_store=event_store), instrument
    )
    if risk_enabled:
        restore_broker_state(broker, snapshot)
    elif quarantine:
        _restore_quarantine_broker_state(broker, snapshot)
    else:
        _restore_core_closed_fence_broker_state(broker, snapshot)
    return broker, event_store


def _requires_quarantine(
    risk_enabled: bool,
    snapshot: BrokerStateSnapshot,
    events: tuple[EventEnvelope, ...],
    binding: CoreLifecycleExpectedGeneration,
) -> bool:
    suspend_events = tuple(
        event for event in events if event.event_type == "core.lifecycle.suspended"
    )
    if len(suspend_events) > 1:
        raise ValueError("Core lifecycle contains unsupported repeated SUSPEND history")
    marker_value: bool | None = None
    if suspend_events:
        marker = suspend_events[0].payload.get("reconciliation_required")
        if marker is not None:
            if type(marker) is not bool:
                raise ValueError("Core SUSPEND quarantine marker is malformed")
            marker_value = marker
    if risk_enabled:
        if suspend_events:
            raise ValueError("Core risk fence reopened without RESUME authority")
        _restore_private_broker(snapshot, events, binding, risk_enabled=True)
        return False
    if marker_value is True:
        _restore_private_broker(
            snapshot, events, binding, risk_enabled=False, quarantine=True
        )
        return True
    if marker_value is False:
        _restore_private_broker(snapshot, events, binding, risk_enabled=False)
        return False

    # Once a legacy SUSPEND has admitted a Core-generated quarantine effect,
    # its receipt-validated event marker keeps that disposition sticky. This
    # is needed when a position-level exit leaves ambiguous orders behind and
    # the resulting snapshot no longer satisfies the legacy DEFAULT validator.
    if any(
        event.event_type == "core.effect.committed"
        and event.payload.get("state_validation_mode") == "quarantine"
        for event in events
    ):
        _restore_private_broker(
            snapshot, events, binding, risk_enabled=False, quarantine=True
        )
        return True

    # Legacy SUSPEND markers predate the durable quarantine disposition. Validate
    # the old snapshot contract first; only its stricter-fence incompatibility
    # may select quarantine.
    _restore_private_broker(snapshot, events, binding, risk_enabled=True)
    try:
        _restore_private_broker(snapshot, events, binding, risk_enabled=False)
    except _ClosedFenceStateIncompatible:
        return True
    return False


def _decode_state(encoded: str, expected_digest: str) -> BrokerStateSnapshot:
    if _sha256(encoded.encode()) != expected_digest:
        raise ValueError("Core broker snapshot digest mismatch")
    value = strict_json_loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("Core broker snapshot must be a JSON object")
    snapshot = BrokerStateSnapshot.from_state(value)
    if _canonical_json(snapshot.to_state()) != encoded:
        raise ValueError("Core broker snapshot is not canonical JSON")
    return snapshot


def _event_from_json(encoded: str) -> EventEnvelope:
    value = strict_json_loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("Core event must be a JSON object")
    try:
        return EventEnvelope(
            event_id=value["event_id"],
            run_id=value["run_id"],
            sequence=value["sequence"],
            timestamp=_timestamp(value["timestamp"], "event timestamp"),
            event_type=value["event_type"],
            source=value["source"],
            payload=value["payload"],
            schema_version=value["schema_version"],
            correlation_id=value.get("correlation_id"),
            causation_id=value.get("causation_id"),
            symbol=value.get("symbol"),
            entity_key=value.get("entity_key"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Core event envelope is malformed") from exc


def _validate_request_receipts(
    connection: sqlite3.Connection,
    aggregate_sequence: int,
    events: tuple[EventEnvelope, ...],
) -> None:
    rows = connection.execute(
        "SELECT request_id,request_kind,request_digest,receipt_json,receipt_sha256,commit_sequence "
        "FROM core_lifecycle_requests ORDER BY commit_sequence,request_id"
    ).fetchall()
    seen_sequences: set[int] = set()
    effect_markers: dict[str, list[EventEnvelope]] = {}
    transition_markers: dict[str, list[EventEnvelope]] = {}
    for event in events:
        if event.event_type == "core.effect.committed":
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                effect_markers.setdefault(request_id, []).append(event)
        elif event.event_type == "core.lifecycle.suspended":
            request_id = event.payload.get("request_id")
            if isinstance(request_id, str):
                transition_markers.setdefault(request_id, []).append(event)
    expected_event_sequence = 1
    suspend_lineage: tuple[int, bool | None] | None = None
    for expected_sequence, row in enumerate(rows, start=1):
        receipt_json = str(row["receipt_json"])
        if (
            _sha256(receipt_json.encode()) != row["receipt_sha256"]
            or int(row["commit_sequence"]) != expected_sequence
            or int(row["commit_sequence"]) in seen_sequences
        ):
            raise ValueError("Core lifecycle request receipt inventory is inconsistent")
        value = strict_json_loads(receipt_json)
        if (
            not isinstance(value, dict)
            or _canonical_json(value) != receipt_json
            or value.get("request_id") != row["request_id"]
            or value.get("kind") != row["request_kind"]
        ):
            raise ValueError("Core lifecycle receipt identity is inconsistent")
        if row["request_kind"] not in {"effect", "transition"}:
            raise ValueError("Core lifecycle request kind is unsupported")
        if (
            not isinstance(row["request_digest"], str)
            or len(row["request_digest"]) != 64
            or any(char not in "0123456789abcdef" for char in row["request_digest"])
        ):
            raise ValueError("Core lifecycle request digest is invalid")
        receipt = (
            _effect_receipt_from_json(receipt_json)
            if row["request_kind"] == "effect"
            else _transition_receipt_from_json(receipt_json)
        )
        if receipt.canonical_commit_sequence != expected_sequence:
            raise ValueError("Core lifecycle receipt sequence is inconsistent")
        markers = (
            effect_markers.pop(str(row["request_id"]), [])
            if row["request_kind"] == "effect"
            else transition_markers.pop(str(row["request_id"]), [])
        )
        if len(markers) != 1:
            raise ValueError("Core lifecycle request is missing its canonical event marker")
        marker = markers[0]
        payload = marker.payload
        if (
            payload.get("instance_id") != receipt.instance_id
            or payload.get("request_digest") != row["request_digest"]
            or payload.get("canonical_commit_sequence") != expected_sequence
            or marker.correlation_id != str(row["request_id"])
        ):
            raise ValueError("Core lifecycle event marker does not match its receipt")
        if row["request_kind"] == "effect":
            state_validation_mode = payload.get("state_validation_mode", "default")
            if state_validation_mode not in {
                "default",
                "core_closed_fence",
                "quarantine",
            }:
                raise ValueError("Core effect state validation marker is unsupported")
            if state_validation_mode != "default":
                if (
                    not isinstance(receipt, CoreEffectCommitReceipt)
                    or receipt.operation != "process_bar"
                    or suspend_lineage is None
                    or receipt.expected_generation != suspend_lineage[0]
                ):
                    raise ValueError("Core effect validation marker has no matching SUSPEND lineage")
                disposition = suspend_lineage[1]
                if state_validation_mode == "quarantine" and disposition is False:
                    raise ValueError("Core quarantine effect contradicts SUSPEND disposition")
                if state_validation_mode == "core_closed_fence" and disposition is not False:
                    raise ValueError("Core closed-fence effect lacks explicit compatible SUSPEND")
            start = payload.get("broker_event_start")
            end = payload.get("broker_event_end")
            count = payload.get("broker_event_count")
            if isinstance(count, int) and not isinstance(count, bool) and count == 0:
                if (
                    start is not None
                    or end is not None
                    or marker.sequence != expected_event_sequence
                ):
                    raise ValueError("Core effect empty event range is inconsistent")
                expected_event_sequence = marker.sequence + 1
            elif (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not isinstance(count, int)
                or isinstance(count, bool)
                or start != expected_event_sequence
                or end < start
                or count != end - start + 1
                or marker.sequence != end + 1
                or tuple(event.sequence for event in events[start - 1 : end])
                != tuple(range(start, end + 1))
            ):
                raise ValueError("Core effect event range is incomplete or inconsistent")
            else:
                expected_event_sequence = marker.sequence + 1
        else:
            if not isinstance(receipt, CoreFenceTransitionReceipt):
                raise ValueError("Core lifecycle transition has the wrong receipt type")
            if (
                marker.event_type != "core.lifecycle.suspended"
                or payload.get("generation") != receipt.resulting_generation
                or marker.sequence != expected_event_sequence
            ):
                raise ValueError("Core SUSPEND event does not match its receipt")
            reconciliation_required = payload.get("reconciliation_required")
            if reconciliation_required is not None and type(reconciliation_required) is not bool:
                raise ValueError("Core SUSPEND reconciliation marker is malformed")
            suspend_lineage = (receipt.resulting_generation, reconciliation_required)
            expected_event_sequence = marker.sequence + 1
        seen_sequences.add(int(row["commit_sequence"]))
    if (
        aggregate_sequence != len(rows)
        or effect_markers
        or transition_markers
        or expected_event_sequence != len(events) + 1
    ):
        raise ValueError("Core aggregate commit sequence does not match receipt inventory")


def _read_saved_receipts(
    connection: sqlite3.Connection,
) -> tuple[CoreSavedCommitReceipt, ...]:
    records: list[CoreSavedCommitReceipt] = []
    for row in connection.execute(
        "SELECT request_id,request_kind,request_digest,receipt_json "
        "FROM core_lifecycle_requests ORDER BY commit_sequence,request_id"
    ):
        kind = str(row["request_kind"])
        receipt_json = str(row["receipt_json"])
        receipt = (
            _effect_receipt_from_json(receipt_json)
            if kind == "effect"
            else _transition_receipt_from_json(receipt_json)
        )
        records.append(
            CoreSavedCommitReceipt(
                str(row["request_id"]),
                str(row["request_digest"]),
                kind,
                receipt,
            )
        )
    return tuple(records)


def _effect_receipt_json(receipt: CoreEffectCommitReceipt) -> str:
    return _canonical_json(
        {
            "request_id": receipt.effect_id,
            "kind": "effect",
            "instance_id": receipt.instance_id,
            "incarnation_id": receipt.incarnation_id,
            "package_id": receipt.package_id,
            "package_digest": receipt.package_digest,
            "writer": {"owner_id": receipt.writer.owner_id, "epoch": receipt.writer.epoch},
            "expected_generation": receipt.expected_generation,
            "canonical_payload_digest": receipt.canonical_payload_digest,
            "operation": receipt.operation,
            "target_ids": receipt.target_ids,
            "authority_refs": [_authority_json(item) for item in receipt.authority_refs],
            "canonical_commit_sequence": receipt.canonical_commit_sequence,
            "committed_at": receipt.committed_at.isoformat(),
        }
    )


def _transition_receipt_json(receipt: CoreFenceTransitionReceipt) -> str:
    return _canonical_json(
        {
            "request_id": receipt.transition_id,
            "kind": "transition",
            "instance_id": receipt.instance_id,
            "incarnation_id": receipt.incarnation_id,
            "package_id": receipt.package_id,
            "package_digest": receipt.package_digest,
            "writer": {"owner_id": receipt.writer.owner_id, "epoch": receipt.writer.epoch},
            "previous_generation": receipt.previous_generation,
            "resulting_generation": receipt.resulting_generation,
            "action": receipt.action.value,
            "canonical_payload_digest": receipt.canonical_payload_digest,
            "authority_refs": [_authority_json(item) for item in receipt.authority_refs],
            "canonical_commit_sequence": receipt.canonical_commit_sequence,
            "committed_at": receipt.committed_at.isoformat(),
        }
    )


def _effect_receipt_from_json(encoded: str) -> CoreEffectCommitReceipt:
    value = _receipt_mapping(encoded, "effect")
    return CoreEffectCommitReceipt(
        value["request_id"],
        value["instance_id"],
        value["incarnation_id"],
        value["package_id"],
        value["package_digest"],
        _writer(value["writer"]),
        _integer(value["expected_generation"], "expected_generation"),
        value["canonical_payload_digest"],
        value["operation"],
        tuple(value["target_ids"]),
        _authorities(value["authority_refs"]),
        _integer(value["canonical_commit_sequence"], "canonical_commit_sequence"),
        _timestamp(value["committed_at"], "committed_at"),
    )


def _transition_receipt_from_json(encoded: str) -> CoreFenceTransitionReceipt:
    value = _receipt_mapping(encoded, "transition")
    return CoreFenceTransitionReceipt(
        value["request_id"],
        value["instance_id"],
        value["incarnation_id"],
        value["package_id"],
        value["package_digest"],
        _writer(value["writer"]),
        _integer(value["previous_generation"], "previous_generation"),
        _integer(value["resulting_generation"], "resulting_generation"),
        CoreLifecycleAction(value["action"]),
        value["canonical_payload_digest"],
        _authorities(value["authority_refs"]),
        _integer(value["canonical_commit_sequence"], "canonical_commit_sequence"),
        _timestamp(value["committed_at"], "committed_at"),
    )


def _receipt_mapping(encoded: str, expected_kind: str) -> dict[str, Any]:
    value = strict_json_loads(encoded)
    if not isinstance(value, dict) or value.get("kind") != expected_kind:
        raise ValueError("stored Core receipt kind is invalid")
    return value


def _writer(value: dict[str, Any]) -> CoreWriterIdentity:
    return CoreWriterIdentity(
        _text(value["owner_id"], "owner_id"), _integer(value["epoch"], "epoch")
    )


def _authorities(values: list[dict[str, Any]]) -> tuple[CoreAuthorityReference, ...]:
    return tuple(
        CoreAuthorityReference(
            _text(item["authority"], "authority"),
            _text(item["reference_id"], "reference_id"),
            _text(item["digest"], "digest"),
        )
        for item in values
    )


def _authority_json(value: CoreAuthorityReference) -> dict[str, str]:
    return {
        "authority": value.authority,
        "reference_id": value.reference_id,
        "digest": value.digest,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO datetime string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _aware_now(clock: Any) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Core lifecycle clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be non-empty text")
    return value


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise TypeError(f"{name} must be numeric")
    result = float(value)
    if not (result == result and abs(result) != float("inf")):
        raise ValueError(f"{name} must be finite")
    return result


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def _run_id(binding: CoreLifecycleExpectedGeneration) -> str:
    return f"core:{binding.instance_id}:{binding.incarnation_id}"


def _user_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _validate_table_columns(connection: sqlite3.Connection) -> None:
    expected = {
        "core_lifecycle_schema": ("version",),
        "core_lifecycle_instance": (
            "singleton",
            "instance_id",
            "incarnation_id",
            "package_id",
            "package_digest",
            "writer_owner_id",
            "writer_epoch",
            "generation",
            "risk_enabled",
            "commit_sequence",
            "broker_state_json",
            "broker_state_sha256",
            "updated_at",
        ),
        "core_lifecycle_authorities": ("authority", "reference_id", "digest", "current"),
        "core_lifecycle_requests": (
            "request_id",
            "request_kind",
            "request_digest",
            "receipt_json",
            "receipt_sha256",
            "commit_sequence",
        ),
        "core_lifecycle_events": ("sequence", "event_json", "event_sha256", "previous_sha256"),
    }
    for table, columns in expected.items():
        actual = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
        if actual != columns:
            raise ValueError(f"Core lifecycle database table shape is unsupported: {table}")


_SCHEMA_SQL = """
CREATE TABLE core_lifecycle_schema(version INTEGER NOT NULL PRIMARY KEY);
CREATE TABLE core_lifecycle_instance(
    singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton=1),
    instance_id TEXT NOT NULL UNIQUE,
    incarnation_id TEXT NOT NULL UNIQUE,
    package_id TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    writer_owner_id TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL CHECK(writer_epoch>=0),
    generation INTEGER NOT NULL CHECK(generation>=0),
    risk_enabled INTEGER NOT NULL CHECK(risk_enabled IN (0,1)),
    commit_sequence INTEGER NOT NULL CHECK(commit_sequence>=0),
    broker_state_json TEXT NOT NULL,
    broker_state_sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE core_lifecycle_authorities(
    authority TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    current INTEGER NOT NULL CHECK(current IN (0,1)),
    PRIMARY KEY(authority,reference_id)
);
CREATE TABLE core_lifecycle_requests(
    request_id TEXT NOT NULL PRIMARY KEY,
    request_kind TEXT NOT NULL CHECK(request_kind IN ('effect','transition')),
    request_digest TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    commit_sequence INTEGER NOT NULL UNIQUE CHECK(commit_sequence>0)
);
CREATE TABLE core_lifecycle_events(
    sequence INTEGER NOT NULL PRIMARY KEY CHECK(sequence>0),
    event_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL
);
"""
