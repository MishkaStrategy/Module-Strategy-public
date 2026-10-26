"""Deterministic, local reference core for an external executor manager.

This module is not a production VM/host manager. It contains no provider,
executor, publisher, signing, or controller credentials and has no concrete
allocation, network, data-mount, remote-store, or host adapter. Every such
boundary is an explicitly injected port; absent or unready ports deny a new
request before the manager journal is written or any backend operation begins.

The manager records intent before every port call and the result afterward.
Port calls always happen outside the SQLite transaction. A process restart
never resumes a pending side effect: replay returns its stored result/pending
phase, and reconciliation is an explicit read-only request. The manager stores
bounded immutable output bytes but never issues or signs a passing receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from scripts.external_admission_ledger import Admission, AdmissionLedger, Claim
from scripts.protected_admission_controller import ProtectedAdmissionController

MAX_SNAPSHOT_BYTES = 10_000_000
MAX_SNAPSHOT_RECORD_BYTES = 16_000_000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ExecutorManagerError(ValueError):
    """A deterministic manager request or local reference operation failed."""


class ManagerDenied(ExecutorManagerError):
    """Required fail-closed configuration or admission is unavailable."""


class ManagerRequestConflict(ExecutorManagerError):
    """A request ID was reused with a different canonical payload digest."""


class ManagerAdmissionReserved(ManagerDenied):
    """A different manager request already owns the admission reservation."""


class _AdmissionNoLongerValid(ManagerDenied):
    """Final expiry check failed before the allocation backend was called."""


class ManagerStateConflict(ExecutorManagerError):
    """A local reference journal compare-and-swap failed."""


class ManagerPhase(StrEnum):
    REQUESTED = "REQUESTED"
    ALLOCATING = "ALLOCATING"
    ALLOCATED = "ALLOCATED"
    AUTHENTICATING = "AUTHENTICATING"
    AUTHENTICATED = "AUTHENTICATED"
    CLAIMING = "CLAIMING"
    CLAIMED = "CLAIMED"
    STARTING = "STARTING"
    STARTED = "STARTED"
    OBSERVING = "OBSERVING"
    OBSERVED = "OBSERVED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    VERIFYING_QUIESCENCE = "VERIFYING_QUIESCENCE"
    QUIESCED = "QUIESCED"
    COLLECTING = "COLLECTING"
    COLLECTED = "COLLECTED"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"
    RECONCILING = "RECONCILING"
    RECONCILED = "RECONCILED"
    DENIED = "DENIED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CLEANUP_RECONCILED = "CLEANUP_RECONCILED"


class WorkloadOutcome(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExecutorManagerRequest:
    request_id: str
    admission_id: str
    executor_id: str
    max_snapshot_bytes: int

    def __post_init__(self) -> None:
        for name in ("request_id", "admission_id", "executor_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ManagerDenied(f"{name} must be a bounded identifier")
        if (
            isinstance(self.max_snapshot_bytes, bool)
            or not isinstance(self.max_snapshot_bytes, int)
            or not 1 <= self.max_snapshot_bytes <= MAX_SNAPSHOT_BYTES
        ):
            raise ManagerDenied("max_snapshot_bytes is outside the fixed safety bound")

    def payload_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "admission_id": self.admission_id,
            "executor_id": self.executor_id,
            "max_snapshot_bytes": self.max_snapshot_bytes,
        }


@dataclass(frozen=True, slots=True)
class Allocation:
    allocation_id: str
    environment_id: str
    image_digest: str


@dataclass(frozen=True, slots=True)
class EnvironmentAttestation:
    allocation_id: str
    environment_id: str
    image_digest: str
    resource_policy_digest: str
    network_policy_digest: str
    material_digest: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class WorkloadHandle:
    allocation_id: str
    handle_id: str


@dataclass(frozen=True, slots=True)
class WorkloadObservation:
    outcome: WorkloadOutcome
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class QuiescenceEvidence:
    allocation_id: str
    scope_id: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class VerifiedQuiescence:
    allocation_id: str
    scope_id: str
    evidence_digest: str
    whole_domain: bool
    authenticated: bool


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    path: str
    file_type: str
    content: bytes
    sha256_before: str
    sha256_after: str


@dataclass(frozen=True, slots=True)
class DestroyEvidence:
    allocation_id: str
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class VerifiedDestroy:
    allocation_id: str
    evidence_digest: str
    destroyed: bool
    authenticated: bool


@dataclass(frozen=True, slots=True)
class ReconciliationEvidence:
    request_id: str
    allocation_operation_key: str
    allocation_id: str | None
    resolved: bool
    operation_settled: bool
    operation_fenced: bool
    whole_domain_quiescent: bool
    allocation_absent: bool
    evidence_digest: str


@dataclass(frozen=True, slots=True)
class VerifiedReconciliation:
    request_id: str
    allocation_operation_key: str
    allocation_id: str | None
    resolved: bool
    operation_settled: bool
    operation_fenced: bool
    whole_domain_quiescent: bool
    allocation_absent: bool
    evidence_digest: str
    authenticated: bool


@dataclass(frozen=True, slots=True)
class ManagerStateRecord:
    request_id: str
    payload_digest: str
    phase: ManagerPhase
    workload_outcome: WorkloadOutcome
    pending_operation: str | None
    revision: int
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ManagerResult:
    request_id: str
    payload_digest: str
    phase: ManagerPhase
    workload_outcome: WorkloadOutcome
    pending_operation: str | None
    revision: int
    snapshot_digest: str | None
    snapshot_size_bytes: int | None


class ManagerStateStore(Protocol):
    def is_ready(self) -> bool: ...

    def get(self, request_id: str) -> ManagerStateRecord | None: ...

    def begin_request(
        self,
        request_id: str,
        payload_digest: str,
        admission_id: str,
        allocation_operation_key: str,
    ) -> tuple[ManagerStateRecord, bool]: ...

    def record_intent(
        self,
        request_id: str,
        payload_digest: str,
        expected_revision: int,
        operation: str,
        phase: ManagerPhase,
    ) -> ManagerStateRecord: ...

    def record_result(
        self,
        request_id: str,
        payload_digest: str,
        expected_revision: int,
        operation: str,
        phase: ManagerPhase,
        workload_outcome: WorkloadOutcome,
        pending_operation: str | None,
        state_patch: Mapping[str, Any],
    ) -> ManagerStateRecord: ...


class AdmissionClaimAuthority(Protocol):
    def is_ready(self) -> bool: ...

    def resolve_admission(self, admission_id: str) -> Admission: ...

    def get_claim(self, admission_id: str) -> Claim | None: ...

    def claim(self, admission_id: str, executor_id: str) -> Claim: ...


class EnvironmentAuthenticator(Protocol):
    def is_ready(self) -> bool: ...

    def authenticate(
        self, allocation: Allocation, admission: Admission
    ) -> EnvironmentAttestation: ...


class ExecutorBackend(Protocol):
    def is_ready(self) -> bool: ...

    def allocate(self, admission: Admission, allocation_operation_key: str) -> Allocation: ...

    def start_once(
        self, allocation: Allocation, admission: Admission, claim: Claim
    ) -> WorkloadHandle: ...

    def observe(self, handle: WorkloadHandle) -> WorkloadObservation: ...

    def stop_and_quiesce(
        self, allocation: Allocation, handle: WorkloadHandle
    ) -> QuiescenceEvidence: ...

    def stop_allocation_and_quiesce(
        self, allocation: Allocation, allocation_operation_key: str
    ) -> QuiescenceEvidence: ...

    def collect_snapshot(
        self,
        allocation: Allocation,
        required_output_paths: tuple[str, ...],
        max_snapshot_bytes: int,
        quiescence: VerifiedQuiescence,
    ) -> tuple[CapturedOutput, ...]: ...

    def destroy(self, allocation: Allocation) -> DestroyEvidence: ...

    def reconcile(
        self,
        request_id: str,
        allocation_operation_key: str,
        allocation_id: str | None,
        state: Mapping[str, Any],
    ) -> ReconciliationEvidence: ...


class ManagerEvidenceVerifier(Protocol):
    def is_ready(self) -> bool: ...

    def verify_quiescence(
        self, evidence: QuiescenceEvidence, allocation: Allocation
    ) -> VerifiedQuiescence: ...

    def verify_destroy(
        self, evidence: DestroyEvidence, allocation: Allocation
    ) -> VerifiedDestroy: ...

    def verify_reconciliation(self, evidence: ReconciliationEvidence) -> VerifiedReconciliation: ...


class ProtectedControllerClaimAuthority:
    """Thin adapter that delegates one-shot claim to the existing controller.

    Grant and credential retrieval are injected callbacks. The manager sees this
    authority port only; it does not receive or store credentials.
    """

    def __init__(
        self,
        *,
        controller: ProtectedAdmissionController | None,
        ledger: AdmissionLedger | None,
        grant_for_admission: Callable[[str], str] | None,
        executor_credential_for_admission: Callable[[str], str] | None,
    ) -> None:
        self._controller = controller
        self._ledger = ledger
        self._grant_for_admission = grant_for_admission
        self._credential_for_admission = executor_credential_for_admission

    def is_ready(self) -> bool:
        if (
            self._controller is None
            or self._ledger is None
            or not callable(self._grant_for_admission)
            or not callable(self._credential_for_admission)
            or not callable(getattr(self._controller, "claim", None))
            or not callable(getattr(self._ledger, "read_snapshot", None))
        ):
            return False
        # The controller owns these injected authenticator/key fields. Inspect
        # only presence and minimum key shape here; no credential is copied.
        key = getattr(self._controller, "_key", None)
        return (
            getattr(self._controller, "_ledger", None) is self._ledger
            and getattr(self._controller, "_provider", None) is not None
            and getattr(self._controller, "_executor_authenticator", None) is not None
            and isinstance(key, bytes)
            and len(key) >= 32
        )

    def resolve_admission(self, admission_id: str) -> Admission:
        if self._ledger is None:
            raise ManagerDenied("admission ledger is not configured")
        admission, _ = self._ledger.read_snapshot(admission_id)
        if not isinstance(admission, Admission):
            raise ManagerDenied("admission is unavailable")
        return admission

    def get_claim(self, admission_id: str) -> Claim | None:
        if self._ledger is None:
            raise ManagerDenied("admission ledger is not configured")
        _, claim = self._ledger.read_snapshot(admission_id)
        if claim is not None and not isinstance(claim, Claim):
            raise ManagerDenied("admission claim status is invalid")
        return claim

    def claim(self, admission_id: str, executor_id: str) -> Claim:
        if not self.is_ready():
            raise ManagerDenied("protected controller claim configuration is incomplete")
        assert self._controller is not None
        assert self._grant_for_admission is not None
        assert self._credential_for_admission is not None
        return self._controller.claim(
            admission_id=admission_id,
            grant=self._grant_for_admission(admission_id),
            credential=self._credential_for_admission(admission_id),
            executor_id=executor_id,
        )


class SQLiteManagerStateStore:
    """File-backed local reference journal/state store; not production storage."""

    def __init__(self, database: str | Path):
        if str(database) == ":memory:":
            raise ExecutorManagerError("manager reference store must be file-backed")
        self.database = Path(database).absolute()
        if self.database.is_symlink():
            raise ExecutorManagerError("manager reference database cannot be a symlink")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS manager_requests (
                    request_id TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    workload_outcome TEXT NOT NULL,
                    pending_operation TEXT,
                    revision INTEGER NOT NULL,
                    state_json TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS manager_events (
                    request_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY(request_id, revision),
                    FOREIGN KEY(request_id) REFERENCES manager_requests(request_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS manager_admission_reservations (
                    admission_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def is_ready(self) -> bool:
        return self.database.is_file() and not self.database.is_symlink()

    def _record(self, row: sqlite3.Row | None) -> ManagerStateRecord | None:
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"])
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
            return ManagerStateRecord(
                request_id=row["request_id"],
                payload_digest=row["payload_digest"],
                phase=ManagerPhase(row["phase"]),
                workload_outcome=WorkloadOutcome(row["workload_outcome"]),
                pending_operation=row["pending_operation"],
                revision=row["revision"],
                state=state,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ExecutorManagerError("manager reference store contains invalid state") from exc

    def get(self, request_id: str) -> ManagerStateRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM manager_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._record(row)

    def begin_request(
        self,
        request_id: str,
        payload_digest: str,
        admission_id: str,
        allocation_operation_key: str,
    ) -> tuple[ManagerStateRecord, bool]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM manager_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                record = self._record(row)
                assert record is not None
                if record.payload_digest != payload_digest:
                    raise ManagerRequestConflict("request_id payload digest conflicts")
                db.execute("COMMIT")
                return record, False
            db.execute(
                "INSERT INTO manager_requests VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    payload_digest,
                    ManagerPhase.REQUESTED.value,
                    WorkloadOutcome.UNDETERMINED.value,
                    "allocate",
                    0,
                    "{}",
                ),
            )
            try:
                db.execute(
                    "INSERT INTO manager_admission_reservations VALUES(?,?)",
                    (admission_id, request_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ManagerAdmissionReserved(
                    "admission is already reserved by another manager request"
                ) from exc
            state = _canonical_json(
                {
                    "admission_id": admission_id,
                    "allocation_operation_key": allocation_operation_key,
                }
            ).decode("ascii")
            db.execute(
                "UPDATE manager_requests SET state_json=? WHERE request_id=?",
                (state, request_id),
            )
            event = _canonical_json(
                {
                    "payload_digest": payload_digest,
                    "phase": ManagerPhase.REQUESTED.value,
                    "admission_id": admission_id,
                    "allocation_operation_key": allocation_operation_key,
                }
            ).decode("ascii")
            db.execute(
                "INSERT INTO manager_events VALUES(?,?,?,?,?)",
                (request_id, 0, "REQUEST", "submit", event),
            )
            db.execute("COMMIT")
            record = db.execute(
                "SELECT * FROM manager_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            result = self._record(record)
            assert result is not None
            return result, True
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def record_intent(
        self,
        request_id: str,
        payload_digest: str,
        expected_revision: int,
        operation: str,
        phase: ManagerPhase,
    ) -> ManagerStateRecord:
        return self._cas_update(
            request_id,
            payload_digest,
            expected_revision,
            event_type="INTENT",
            operation=operation,
            phase=phase,
            workload_outcome=None,
            pending_operation=operation,
            state_patch={},
        )

    def record_result(
        self,
        request_id: str,
        payload_digest: str,
        expected_revision: int,
        operation: str,
        phase: ManagerPhase,
        workload_outcome: WorkloadOutcome,
        pending_operation: str | None,
        state_patch: Mapping[str, Any],
    ) -> ManagerStateRecord:
        return self._cas_update(
            request_id,
            payload_digest,
            expected_revision,
            event_type="RESULT",
            operation=operation,
            phase=phase,
            workload_outcome=workload_outcome,
            pending_operation=pending_operation,
            state_patch=state_patch,
        )

    def _cas_update(
        self,
        request_id: str,
        payload_digest: str,
        expected_revision: int,
        *,
        event_type: str,
        operation: str,
        phase: ManagerPhase,
        workload_outcome: WorkloadOutcome | None,
        pending_operation: str | None,
        state_patch: Mapping[str, Any],
    ) -> ManagerStateRecord:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM manager_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            record = self._record(row)
            if record is None or record.payload_digest != payload_digest:
                raise ManagerStateConflict("manager request identity changed")
            if record.revision != expected_revision:
                raise ManagerStateConflict("manager state compare-and-swap failed")
            for immutable in ("snapshot_bytes_b64", "snapshot_digest", "snapshot_size_bytes"):
                if immutable in record.state and immutable in state_patch:
                    if record.state[immutable] != state_patch[immutable]:
                        raise ManagerStateConflict("immutable snapshot state cannot be replaced")
            state = dict(record.state)
            state.update(state_patch)
            encoded_state = _canonical_json(state).decode("ascii")
            new_revision = expected_revision + 1
            new_outcome = workload_outcome or record.workload_outcome
            cursor = db.execute(
                """UPDATE manager_requests
                   SET phase=?, workload_outcome=?, pending_operation=?, revision=?, state_json=?
                   WHERE request_id=? AND payload_digest=? AND revision=?""",
                (
                    phase.value,
                    new_outcome.value,
                    pending_operation,
                    new_revision,
                    encoded_state,
                    request_id,
                    payload_digest,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ManagerStateConflict("manager state compare-and-swap failed")
            event = _canonical_json(
                {
                    "phase": phase.value,
                    "pending_operation": pending_operation,
                    "workload_outcome": new_outcome.value,
                    "state_patch": dict(state_patch),
                }
            ).decode("ascii")
            db.execute(
                "INSERT INTO manager_events VALUES(?,?,?,?,?)",
                (request_id, new_revision, event_type, operation, event),
            )
            db.execute("COMMIT")
            updated = db.execute(
                "SELECT * FROM manager_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            result = self._record(updated)
            assert result is not None
            return result
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def event_count(self, request_id: str) -> int:
        """Local reference inspection helper; not part of a production API."""
        with self._connect() as db:
            return int(
                db.execute(
                    "SELECT COUNT(*) FROM manager_events WHERE request_id=?", (request_id,)
                ).fetchone()[0]
            )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ManagerDenied("manager request or state is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_mapping(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_json(dict(value)))


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ManagerDenied(f"admission {label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManagerDenied(f"admission {label} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ManagerDenied(f"admission {label} must be a UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _allocation_key(request_id: str, admission_id: str, payload_digest: str) -> str:
    material = f"{request_id}\0{admission_id}\0{payload_digest}".encode("ascii")
    return "alloc-" + _sha256(material)


def _stored_allocation_id(state: Mapping[str, Any]) -> str | None:
    allocation = state.get("allocation")
    allocation_id: str | None = None
    if allocation is not None:
        if not isinstance(allocation, dict):
            raise ManagerDenied("persisted allocation record is invalid")
        allocation_id = allocation.get("allocation_id")
        if not isinstance(allocation_id, str) or not _ID.fullmatch(allocation_id):
            raise ManagerDenied("persisted allocation identity is invalid")
    recovered_id = state.get("recovered_allocation_id")
    if "recovered_allocation_id" in state and (
        not isinstance(recovered_id, str) or not _ID.fullmatch(recovered_id)
    ):
        raise ManagerDenied("persisted recovered allocation identity is invalid")
    if allocation_id is not None and recovered_id is not None and allocation_id != recovered_id:
        raise ManagerDenied("persisted allocation identities conflict")
    return allocation_id or recovered_id


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _is_ready(port: object | None) -> bool:
    if port is None:
        return False
    ready = getattr(port, "is_ready", None)
    if not callable(ready):
        return False
    try:
        return ready() is True
    except Exception:
        return False


class DeterministicExecutorManager:
    """Local deterministic manager core. All operational ports are injected."""

    def __init__(
        self,
        *,
        state_store: ManagerStateStore | None,
        backend: ExecutorBackend | None,
        environment_authenticator: EnvironmentAuthenticator | None,
        admission_authority: AdmissionClaimAuthority | None,
        evidence_verifier: ManagerEvidenceVerifier | None,
        utc_clock: Callable[[], datetime] | None,
    ) -> None:
        self._store = state_store
        self._backend = backend
        self._environment_authenticator = environment_authenticator
        self._admission_authority = admission_authority
        self._evidence_verifier = evidence_verifier
        self._utc_clock = utc_clock

    def submit(self, request: ExecutorManagerRequest) -> ManagerResult:
        if not isinstance(request, ExecutorManagerRequest):
            raise ManagerDenied("request must use the closed ExecutorManagerRequest type")
        if not _is_ready(self._store):
            raise ManagerDenied("manager state store is not configured and ready")
        assert self._store is not None
        payload_digest = _digest_mapping(request.payload_mapping())

        existing = self._store.get(request.request_id)
        if existing is not None:
            if existing.payload_digest != payload_digest:
                raise ManagerRequestConflict("request_id payload digest conflicts")
            return self._result(existing)

        self._require_operational_ports()
        assert self._admission_authority is not None
        try:
            admission = self._admission_authority.resolve_admission(request.admission_id)
        except Exception as exc:
            raise ManagerDenied("immutable admission is unavailable") from exc
        self._validate_admission(request, admission)
        self._preflight_admission(admission)
        operation_key = _allocation_key(request.request_id, admission.admission_id, payload_digest)
        record, created = self._store.begin_request(
            request.request_id, payload_digest, admission.admission_id, operation_key
        )
        if not created:
            if record.payload_digest != payload_digest:
                raise ManagerRequestConflict("request_id payload digest conflicts")
            return self._result(record)
        try:
            self._preflight_admission(admission)
        except ManagerDenied as exc:
            denied = self._store.record_result(
                record.request_id,
                record.payload_digest,
                record.revision,
                "admission_preflight",
                ManagerPhase.DENIED,
                record.workload_outcome,
                None,
                {"denial_type": type(exc).__name__},
            )
            return self._result(denied)
        return self._result(self._execute_new(request, admission, record))

    def reconcile_pending(self, request_id: str) -> ManagerResult:
        """Explicit read-only reconciliation; never resumes a pending side effect."""
        if (
            not _is_ready(self._store)
            or not _is_ready(self._backend)
            or not _is_ready(self._evidence_verifier)
        ):
            raise ManagerDenied("state, backend and verifier ports are required for reconciliation")
        assert self._store is not None
        assert self._backend is not None
        assert self._evidence_verifier is not None
        record = self._store.get(request_id)
        if record is None:
            raise ManagerDenied("unknown manager request")
        if record.phase in {
            ManagerPhase.RECONCILED,
            ManagerPhase.DENIED,
            ManagerPhase.CLEANUP_RECONCILED,
        }:
            return self._result(record)
        if record.phase is not ManagerPhase.RECONCILIATION_REQUIRED:
            raise ManagerDenied("recovery requires RECONCILIATION_REQUIRED state")
        if record.pending_operation == "reconcile":
            # A persisted reconciliation intent/result is never replayed
            # automatically; another authority-level decision is required.
            return self._result(record)
        operation_key = record.state.get("allocation_operation_key")
        if not isinstance(operation_key, str) or not _ID.fullmatch(operation_key):
            raise ManagerDenied("persisted allocation operation key is invalid")
        known_allocation_id = _stored_allocation_id(record.state)
        intent = self._intent(record, "reconcile", ManagerPhase.RECONCILING)
        try:
            evidence = self._backend.reconcile(
                request_id, operation_key, known_allocation_id, intent.state
            )
            if not isinstance(evidence, ReconciliationEvidence):
                raise ExecutorManagerError("backend returned invalid reconciliation evidence")
            if (
                evidence.request_id != request_id
                or evidence.allocation_operation_key != operation_key
                or (
                    known_allocation_id is not None
                    and evidence.allocation_id != known_allocation_id
                )
            ):
                raise ExecutorManagerError(
                    "reconciliation evidence identity does not match request"
                )
            verified = self._evidence_verifier.verify_reconciliation(evidence)
            if (
                not self._verified_reconciliation(
                    request_id, operation_key, known_allocation_id, verified
                )
                or verified.allocation_id != evidence.allocation_id
            ):
                raise ExecutorManagerError("reconciliation evidence is unresolved or mismatched")
        except Exception as exc:
            result = self._reconciliation_required(intent, "reconcile", exc)
            return self._result(result)
        if verified.allocation_absent is not True:
            patch: dict[str, Any] = {"reconciliation_digest": verified.evidence_digest}
            if verified.allocation_id is not None:
                patch["recovered_allocation_id"] = verified.allocation_id
                patch["verified_reconciliation"] = {
                    "request_id": request_id,
                    "allocation_operation_key": operation_key,
                    "allocation_id": verified.allocation_id,
                    "operation_settled": True,
                    "operation_fenced": True,
                    "whole_domain_quiescent": verified.whole_domain_quiescent,
                    "allocation_absent": False,
                    "evidence_digest": verified.evidence_digest,
                }
            result = self._store.record_result(
                request_id,
                record.payload_digest,
                intent.revision,
                "reconcile",
                ManagerPhase.RECONCILIATION_REQUIRED,
                record.workload_outcome,
                "cleanup_required" if verified.allocation_id is not None else "reconcile",
                patch,
            )
            return self._result(result)
        result = self._store.record_result(
            request_id,
            record.payload_digest,
            intent.revision,
            "reconcile",
            ManagerPhase.RECONCILED,
            record.workload_outcome,
            None,
            {"reconciliation_digest": verified.evidence_digest},
        )
        return self._result(result)

    def cleanup(self, request_id: str) -> ManagerResult:
        """Explicitly clean up one known allocation; never resumes on submit replay."""
        if (
            not _is_ready(self._store)
            or not _is_ready(self._backend)
            or not _is_ready(self._admission_authority)
            or not _is_ready(self._evidence_verifier)
        ):
            raise ManagerDenied(
                "state, backend, admission and verifier ports are required for cleanup"
            )
        assert self._store is not None
        assert self._backend is not None
        assert self._admission_authority is not None
        assert self._evidence_verifier is not None
        record = self._store.get(request_id)
        if record is None:
            raise ManagerDenied("unknown manager request")
        if record.phase in {
            ManagerPhase.RECONCILED,
            ManagerPhase.DENIED,
            ManagerPhase.CLEANUP_RECONCILED,
        }:
            return self._result(record)
        if record.phase is not ManagerPhase.RECONCILIATION_REQUIRED:
            raise ManagerDenied("cleanup requires RECONCILIATION_REQUIRED state")
        if record.pending_operation != "cleanup_required":
            raise ManagerDenied("cleanup requires verified present-allocation reconciliation")
        if record.pending_operation in {
            "cleanup_stop_allocation",
            "cleanup_verify_quiescence",
            "cleanup_destroy",
            "cleanup_verify_destroy",
            "cleanup_reconcile",
        }:
            return self._result(record)

        operation_key = record.state.get("allocation_operation_key")
        admission_id = record.state.get("admission_id")
        if not isinstance(operation_key, str) or not _ID.fullmatch(operation_key):
            raise ManagerDenied("persisted allocation operation key is invalid")
        if not isinstance(admission_id, str) or not _ID.fullmatch(admission_id):
            raise ManagerDenied("persisted admission identity is invalid")
        allocation_mapping = record.state.get("allocation")
        known_allocation_id = _stored_allocation_id(record.state)
        if isinstance(allocation_mapping, dict):
            allocation_id = allocation_mapping.get("allocation_id")
            environment_id = allocation_mapping.get("environment_id")
            image_digest = allocation_mapping.get("image_digest")
            if (
                not isinstance(allocation_id, str)
                or not isinstance(environment_id, str)
                or not isinstance(image_digest, str)
            ):
                raise ManagerDenied("persisted allocation fields are invalid")
            allocation = Allocation(allocation_id, environment_id, image_digest)
        elif isinstance(known_allocation_id, str):
            try:
                admission = self._admission_authority.resolve_admission(admission_id)
            except Exception as exc:
                raise ManagerDenied("immutable admission is unavailable for cleanup") from exc
            allocation = Allocation(
                known_allocation_id, admission.environment_id, admission.image_digest
            )
        else:
            raise ManagerDenied(
                "allocation identity is unknown; reconcile by operation key before cleanup"
            )
        try:
            admission = self._admission_authority.resolve_admission(admission_id)
        except Exception as exc:
            raise ManagerDenied("immutable admission is unavailable for cleanup") from exc
        if not isinstance(admission, Admission) or admission.admission_id != admission_id:
            raise ManagerDenied("persisted admission identity cannot be verified")
        self._allocation_result(allocation, admission)
        proof = record.state.get("verified_reconciliation")
        quiescent_proof = proof.get("whole_domain_quiescent") if isinstance(proof, dict) else None
        if (
            not isinstance(proof, dict)
            or proof.get("request_id") != request_id
            or proof.get("allocation_operation_key") != operation_key
            or proof.get("allocation_id") != allocation.allocation_id
            or proof.get("operation_settled") is not True
            or proof.get("operation_fenced") is not True
            or (quiescent_proof is not True and quiescent_proof is not False)
            or proof.get("allocation_absent") is not False
            or not _is_digest(proof.get("evidence_digest"))
        ):
            raise ManagerDenied("cleanup lacks verified settled and fenced allocation evidence")

        record, quiescence = self._effect(
            record,
            "cleanup_stop_allocation",
            ManagerPhase.STOPPING,
            ManagerPhase.STOPPED,
            "cleanup_verify_quiescence",
            lambda: self._backend.stop_allocation_and_quiesce(allocation, operation_key),
            lambda value: self._quiescence_result(value, allocation),
        )
        if quiescence is None:
            return self._result(record)

        record, verified_quiescence = self._effect(
            record,
            "cleanup_verify_quiescence",
            ManagerPhase.VERIFYING_QUIESCENCE,
            ManagerPhase.QUIESCED,
            "cleanup_destroy",
            lambda: self._evidence_verifier.verify_quiescence(quiescence, allocation),
            lambda value: self._verified_quiescence_result(value, allocation),
        )
        if verified_quiescence is None:
            return self._result(record)

        record, destroyed = self._effect(
            record,
            "cleanup_destroy",
            ManagerPhase.DESTROYING,
            ManagerPhase.DESTROYED,
            "cleanup_verify_destroy",
            lambda: self._backend.destroy(allocation),
            lambda value: self._destroy_result(value, allocation),
        )
        if destroyed is None:
            return self._result(record)

        record, verified_destroy = self._effect(
            record,
            "cleanup_verify_destroy",
            ManagerPhase.DESTROYING,
            ManagerPhase.DESTROYED,
            "cleanup_reconcile",
            lambda: self._evidence_verifier.verify_destroy(destroyed, allocation),
            lambda value: self._verified_destroy_result(value, allocation),
        )
        if verified_destroy is None:
            return self._result(record)

        record, _ = self._effect(
            record,
            "cleanup_reconcile",
            ManagerPhase.RECONCILING,
            ManagerPhase.CLEANUP_RECONCILED,
            None,
            lambda: self._backend.reconcile(
                request_id, operation_key, allocation.allocation_id, record.state
            ),
            lambda value: self._reconciliation_result(value, request_id, operation_key, allocation),
        )
        return self._result(record)

    def _require_operational_ports(self) -> None:
        ports = (
            self._backend,
            self._environment_authenticator,
            self._admission_authority,
            self._evidence_verifier,
        )
        if not callable(self._utc_clock) or not all(_is_ready(port) for port in ports):
            raise ManagerDenied("backend, authentication, claim and verifier ports are required")

    def _preflight_admission(self, admission: Admission) -> None:
        assert self._admission_authority is not None
        try:
            claim = self._admission_authority.get_claim(admission.admission_id)
        except Exception as exc:
            raise ManagerDenied("authoritative admission claim status is unavailable") from exc
        if claim is not None:
            raise ManagerDenied("admission has already been claimed")
        self._check_admission_time(admission)

    def _check_admission_time(self, admission: Admission) -> None:
        assert self._utc_clock is not None
        try:
            now = self._utc_clock()
        except Exception as exc:
            raise ManagerDenied("trusted UTC clock is unavailable") from exc
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ManagerDenied("trusted clock must return an aware UTC datetime")
        now = now.astimezone(timezone.utc)
        issued_at = _utc_timestamp(admission.issued_at, "issued_at")
        expires_at = _utc_timestamp(admission.expires_at, "expires_at")
        if now < issued_at:
            raise ManagerDenied("admission is not yet valid")
        if now >= expires_at:
            raise ManagerDenied("admission has expired")

    def _allocate_with_final_expiry_check(
        self, admission: Admission, allocation_operation_key: str
    ) -> Allocation:
        assert self._backend is not None
        try:
            self._check_admission_time(admission)
        except ManagerDenied as exc:
            raise _AdmissionNoLongerValid(str(exc)) from exc
        return self._backend.allocate(admission, allocation_operation_key)

    def _validate_admission(self, request: ExecutorManagerRequest, admission: Admission) -> None:
        if not isinstance(admission, Admission) or admission.admission_id != request.admission_id:
            raise ManagerDenied("resolved admission identity is invalid")
        if not admission.required_output_paths:
            raise ManagerDenied("admission has no exact required output inventory")

    def _execute_new(
        self,
        request: ExecutorManagerRequest,
        admission: Admission,
        record: ManagerStateRecord,
    ) -> ManagerStateRecord:
        assert self._backend is not None
        assert self._environment_authenticator is not None
        assert self._admission_authority is not None
        assert self._evidence_verifier is not None

        record, allocation = self._effect(
            record,
            "allocate",
            ManagerPhase.ALLOCATING,
            ManagerPhase.ALLOCATED,
            "authenticate",
            lambda: self._allocate_with_final_expiry_check(
                admission, record.state["allocation_operation_key"]
            ),
            lambda value: self._allocation_result(value, admission),
        )
        if allocation is None:
            return record

        record, attestation = self._effect(
            record,
            "authenticate",
            ManagerPhase.AUTHENTICATING,
            ManagerPhase.AUTHENTICATED,
            "claim",
            lambda: self._environment_authenticator.authenticate(allocation, admission),
            lambda value: self._attestation_result(value, allocation, admission),
        )
        if attestation is None:
            return record

        record, claim = self._effect(
            record,
            "claim",
            ManagerPhase.CLAIMING,
            ManagerPhase.CLAIMED,
            "start_once",
            lambda: self._admission_authority.claim(admission.admission_id, request.executor_id),
            lambda value: self._claim_result(value, admission, request),
        )
        if claim is None:
            return record

        record, handle = self._effect(
            record,
            "start_once",
            ManagerPhase.STARTING,
            ManagerPhase.STARTED,
            "observe",
            lambda: self._backend.start_once(allocation, admission, claim),
            lambda value: self._handle_result(value, allocation),
        )
        if handle is None:
            return record

        record, observation = self._effect(
            record,
            "observe",
            ManagerPhase.OBSERVING,
            ManagerPhase.OBSERVED,
            "stop_quiesce",
            lambda: self._backend.observe(handle),
            self._observation_result,
        )
        if observation is None:
            return record

        record, quiescence = self._effect(
            record,
            "stop_quiesce",
            ManagerPhase.STOPPING,
            ManagerPhase.STOPPED,
            "verify_quiescence",
            lambda: self._backend.stop_and_quiesce(allocation, handle),
            lambda value: self._quiescence_result(value, allocation),
        )
        if quiescence is None:
            return record

        record, verified_quiescence = self._effect(
            record,
            "verify_quiescence",
            ManagerPhase.VERIFYING_QUIESCENCE,
            ManagerPhase.QUIESCED,
            "collect_snapshot",
            lambda: self._evidence_verifier.verify_quiescence(quiescence, allocation),
            lambda value: self._verified_quiescence_result(value, allocation),
        )
        if verified_quiescence is None:
            return record

        record, snapshot = self._effect(
            record,
            "collect_snapshot",
            ManagerPhase.COLLECTING,
            ManagerPhase.COLLECTED,
            "destroy",
            lambda: self._backend.collect_snapshot(
                allocation,
                admission.required_output_paths,
                request.max_snapshot_bytes,
                verified_quiescence,
            ),
            lambda value: self._snapshot_result(value, allocation, admission, request),
        )
        if snapshot is None:
            return record

        record, destroyed = self._effect(
            record,
            "destroy",
            ManagerPhase.DESTROYING,
            ManagerPhase.DESTROYED,
            "verify_destroy",
            lambda: self._backend.destroy(allocation),
            lambda value: self._destroy_result(value, allocation),
        )
        if destroyed is None:
            return record

        record, verified_destroy = self._effect(
            record,
            "verify_destroy",
            ManagerPhase.DESTROYING,
            ManagerPhase.DESTROYED,
            "reconcile",
            lambda: self._evidence_verifier.verify_destroy(destroyed, allocation),
            lambda value: self._verified_destroy_result(value, allocation),
        )
        if verified_destroy is None:
            return record

        record, reconciliation = self._effect(
            record,
            "reconcile",
            ManagerPhase.RECONCILING,
            ManagerPhase.RECONCILED,
            None,
            lambda: self._backend.reconcile(
                request.request_id,
                record.state["allocation_operation_key"],
                allocation.allocation_id,
                record.state,
            ),
            lambda value: self._reconciliation_result(
                value,
                request.request_id,
                record.state["allocation_operation_key"],
                allocation,
            ),
        )
        return record

    def _effect(
        self,
        record: ManagerStateRecord,
        operation: str,
        intent_phase: ManagerPhase,
        result_phase: ManagerPhase,
        next_operation: str | None,
        call: Callable[[], Any],
        validate: Callable[[Any], dict[str, Any]],
    ) -> tuple[ManagerStateRecord, Any | None]:
        assert self._store is not None
        intent = self._intent(record, operation, intent_phase)
        try:
            output = call()
            patch = validate(output)
            outcome = record.workload_outcome
            if "_workload_outcome" in patch:
                outcome = WorkloadOutcome(patch.pop("_workload_outcome"))
            updated = self._store.record_result(
                record.request_id,
                record.payload_digest,
                intent.revision,
                operation,
                result_phase,
                outcome,
                next_operation,
                patch,
            )
            return updated, output
        except Exception as exc:
            if isinstance(exc, _AdmissionNoLongerValid):
                denied = self._store.record_result(
                    record.request_id,
                    record.payload_digest,
                    intent.revision,
                    operation,
                    ManagerPhase.DENIED,
                    record.workload_outcome,
                    None,
                    {"denial_type": type(exc).__name__},
                )
                return denied, None
            return self._reconciliation_required(intent, operation, exc), None

    def _intent(
        self, record: ManagerStateRecord, operation: str, phase: ManagerPhase
    ) -> ManagerStateRecord:
        assert self._store is not None
        return self._store.record_intent(
            record.request_id,
            record.payload_digest,
            record.revision,
            operation,
            phase,
        )

    def _reconciliation_required(
        self, intent: ManagerStateRecord, operation: str, error: Exception
    ) -> ManagerStateRecord:
        assert self._store is not None
        return self._store.record_result(
            intent.request_id,
            intent.payload_digest,
            intent.revision,
            operation,
            ManagerPhase.RECONCILIATION_REQUIRED,
            intent.workload_outcome,
            operation,
            {"last_error_type": type(error).__name__},
        )

    def _allocation_result(self, value: Any, admission: Admission) -> dict[str, Any]:
        if not isinstance(value, Allocation):
            raise ExecutorManagerError("backend returned invalid allocation")
        if (
            not _ID.fullmatch(value.allocation_id)
            or value.environment_id != admission.environment_id
            or value.image_digest != admission.image_digest
        ):
            raise ExecutorManagerError("allocation does not match the bound admission")
        return {"allocation": _allocation_mapping(value)}

    def _attestation_result(
        self, value: Any, allocation: Allocation, admission: Admission
    ) -> dict[str, Any]:
        if not isinstance(value, EnvironmentAttestation):
            raise ExecutorManagerError("environment authenticator returned invalid evidence")
        resource_digest = _digest_mapping(admission.resource_policy.to_mapping())
        network_digest = _digest_mapping(admission.network_policy.to_mapping())
        if (
            value.allocation_id != allocation.allocation_id
            or value.environment_id != admission.environment_id
            or value.image_digest != admission.image_digest
            or value.resource_policy_digest != resource_digest
            or value.network_policy_digest != network_digest
            or value.material_digest != admission.material_digest
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("authenticated environment differs from admission")
        return {"environment_evidence_digest": value.evidence_digest}

    def _claim_result(
        self, value: Any, admission: Admission, request: ExecutorManagerRequest
    ) -> dict[str, Any]:
        if (
            not isinstance(value, Claim)
            or value.admission_id != admission.admission_id
            or value.executor_id != request.executor_id
        ):
            raise ExecutorManagerError("one-shot claim authority returned an invalid claim")
        return {"claim": value.to_mapping()}

    def _handle_result(self, value: Any, allocation: Allocation) -> dict[str, Any]:
        if (
            not isinstance(value, WorkloadHandle)
            or value.allocation_id != allocation.allocation_id
            or not _ID.fullmatch(value.handle_id)
        ):
            raise ExecutorManagerError("backend returned an invalid start-once handle")
        return {
            "workload_handle": {"allocation_id": value.allocation_id, "handle_id": value.handle_id}
        }

    def _observation_result(self, value: Any) -> dict[str, Any]:
        if (
            not isinstance(value, WorkloadObservation)
            or not isinstance(value.outcome, WorkloadOutcome)
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("backend returned an invalid workload observation")
        return {
            "observation_digest": value.evidence_digest,
            "_workload_outcome": value.outcome.value,
        }

    def _quiescence_result(self, value: Any, allocation: Allocation) -> dict[str, Any]:
        if (
            not isinstance(value, QuiescenceEvidence)
            or value.allocation_id != allocation.allocation_id
            or not _ID.fullmatch(value.scope_id)
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("backend returned invalid quiescence evidence")
        return {"quiescence_evidence_digest": value.evidence_digest}

    def _verified_quiescence_result(self, value: Any, allocation: Allocation) -> dict[str, Any]:
        if (
            not isinstance(value, VerifiedQuiescence)
            or value.allocation_id != allocation.allocation_id
            or value.whole_domain is not True
            or value.authenticated is not True
            or not _ID.fullmatch(value.scope_id)
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("whole-domain quiescence is not authenticated")
        return {"verified_quiescence_digest": value.evidence_digest}

    def _snapshot_result(
        self,
        value: Any,
        allocation: Allocation,
        admission: Admission,
        request: ExecutorManagerRequest,
    ) -> dict[str, Any]:
        if not isinstance(value, tuple) or any(not isinstance(x, CapturedOutput) for x in value):
            raise ExecutorManagerError("snapshot collector returned an invalid output set")
        expected = admission.required_output_paths
        expected_folded = {path.casefold() for path in expected}
        seen: set[str] = set()
        files: list[dict[str, Any]] = []
        raw_size = 0
        for output in value:
            if (
                output.file_type != "regular"
                or not isinstance(output.path, str)
                or output.path not in expected
                or output.path.casefold() not in expected_folded
                or output.path.casefold() in seen
                or type(output.content) is not bytes
            ):
                raise ExecutorManagerError("snapshot contains an unexpected or special path")
            seen.add(output.path.casefold())
            digest = _sha256(output.content)
            if output.sha256_before != digest or output.sha256_after != digest:
                raise ExecutorManagerError("output bytes changed during snapshot collection")
            raw_size += len(output.content)
            if raw_size > request.max_snapshot_bytes:
                raise ExecutorManagerError("snapshot exceeds its authorized byte bound")
            files.append(
                {
                    "path": output.path,
                    "sha256": digest,
                    "size_bytes": len(output.content),
                    "content_b64": base64.b64encode(output.content).decode("ascii"),
                }
            )
        if seen != expected_folded:
            raise ExecutorManagerError("snapshot does not match the exact required output set")
        files.sort(key=lambda item: item["path"])
        snapshot = {
            "schema_version": 1,
            "allocation_id": allocation.allocation_id,
            "admission_id": admission.admission_id,
            "files": files,
        }
        encoded = _canonical_json(snapshot)
        if len(encoded) > MAX_SNAPSHOT_RECORD_BYTES:
            raise ExecutorManagerError("serialized snapshot exceeds the hard record bound")
        return {
            "snapshot_bytes_b64": base64.b64encode(encoded).decode("ascii"),
            "snapshot_digest": _sha256(encoded),
            "snapshot_size_bytes": raw_size,
        }

    def _destroy_result(self, value: Any, allocation: Allocation) -> dict[str, Any]:
        if (
            not isinstance(value, DestroyEvidence)
            or value.allocation_id != allocation.allocation_id
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("backend returned invalid destroy evidence")
        return {"destroy_evidence_digest": value.evidence_digest}

    def _verified_destroy_result(self, value: Any, allocation: Allocation) -> dict[str, Any]:
        if (
            not isinstance(value, VerifiedDestroy)
            or value.allocation_id != allocation.allocation_id
            or value.destroyed is not True
            or value.authenticated is not True
            or not _is_digest(value.evidence_digest)
        ):
            raise ExecutorManagerError("destroy evidence is not authenticated")
        return {"verified_destroy_digest": value.evidence_digest}

    def _reconciliation_result(
        self,
        value: Any,
        request_id: str,
        allocation_operation_key: str,
        allocation: Allocation,
    ) -> dict[str, Any]:
        if not isinstance(value, ReconciliationEvidence):
            raise ExecutorManagerError("backend returned invalid reconciliation evidence")
        if (
            value.request_id != request_id
            or value.allocation_operation_key != allocation_operation_key
            or value.allocation_id != allocation.allocation_id
        ):
            raise ExecutorManagerError("reconciliation evidence identity does not match allocation")
        verified = self._evidence_verifier.verify_reconciliation(value)  # type: ignore[union-attr]
        if not self._verified_reconciliation(
            request_id, allocation_operation_key, allocation.allocation_id, verified
        ):
            raise ExecutorManagerError("allocation reconciliation is unresolved")
        if verified.allocation_absent is not True:
            raise ExecutorManagerError("allocation is still present after destroy")
        return {"reconciliation_digest": verified.evidence_digest}

    def _verified_reconciliation(
        self,
        request_id: str,
        allocation_operation_key: str,
        expected_allocation_id: str | None,
        value: Any,
    ) -> bool:
        if (
            not isinstance(value, VerifiedReconciliation)
            or value.request_id != request_id
            or value.allocation_operation_key != allocation_operation_key
            or value.resolved is not True
            or value.operation_settled is not True
            or value.operation_fenced is not True
            or (
                value.whole_domain_quiescent is not True
                and value.whole_domain_quiescent is not False
            )
            or (value.allocation_absent is not True and value.allocation_absent is not False)
            or value.authenticated is not True
            or not _is_digest(value.evidence_digest)
        ):
            return False
        if expected_allocation_id is not None:
            if value.allocation_id != expected_allocation_id:
                return False
        elif value.allocation_id is not None and (
            not isinstance(value.allocation_id, str) or not _ID.fullmatch(value.allocation_id)
        ):
            return False
        if value.allocation_absent is True:
            return True
        return value.allocation_id is not None

    def _result(self, record: ManagerStateRecord) -> ManagerResult:
        digest = record.state.get("snapshot_digest")
        size = record.state.get("snapshot_size_bytes")
        return ManagerResult(
            request_id=record.request_id,
            payload_digest=record.payload_digest,
            phase=record.phase,
            workload_outcome=record.workload_outcome,
            pending_operation=record.pending_operation,
            revision=record.revision,
            snapshot_digest=digest if isinstance(digest, str) else None,
            snapshot_size_bytes=size if isinstance(size, int) else None,
        )


def _allocation_mapping(value: Allocation) -> dict[str, str]:
    return {
        "allocation_id": value.allocation_id,
        "environment_id": value.environment_id,
        "image_digest": value.image_digest,
    }
