from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_ingestion.enums import RunState

from .contracts import JobOperation
from .control_plane_security import ApiTokenRecord, ApiTokenSnapshot, TokenSnapshotState
from .control_plane_store import AuditContext, ControlPlaneStateError, SQLiteControlPlaneStore
from .job_store import SQLiteJobStore
from .lease_store import SQLiteJobLeaseStore
from .repair_confirmation_store import SQLiteRepairConfirmationStore
from .security import ApiRole

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_AUDIT_KEY = b"market-data-storage-pressure-benchmark-audit-key"
_TOKEN = "market-data-storage-pressure-benchmark-token-000000000000"
_MAX_SCALE = 10_000
_MAX_REPEATS = 5
_MAX_ATTEMPTS = 256
_BOUNDARY_REQUEST_BYTES = 32 * 1024
_BOUNDARY_AUDIT_FIELDS = 32
_BOUNDARY_AUDIT_FIELD_BYTES = 1024


@dataclass(frozen=True, slots=True)
class _Footprint:
    database_bytes: int
    wal_bytes: int
    shm_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.database_bytes + self.wal_bytes + self.shm_bytes


@dataclass(frozen=True, slots=True)
class _OperationalState:
    jobs: int
    events: int
    leases: int
    confirmations: int
    idempotency_rows: int

    def durable_counts(self) -> tuple[int, int, int, int]:
        return self.jobs, self.events, self.leases, self.confirmations


@dataclass(frozen=True, slots=True)
class _SecurityState:
    generation: int
    audit_records: int


class _BoundedJobStore(SQLiteJobStore):
    """Proof-only store that applies a SQLite page ceiling on every owner connection."""

    def __init__(self, path: Path) -> None:
        self.max_page_count: int | None = None
        super().__init__(path)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with super()._connection(write=write) as connection:
            if self.max_page_count is not None:
                connection.execute(f"PRAGMA max_page_count = {self.max_page_count}")
            yield connection


class _BoundedControlPlaneStore(SQLiteControlPlaneStore):
    """Proof-only security store with the same per-connection SQLite page ceiling."""

    def __init__(self, path: Path, *, audit_key: bytes) -> None:
        self.max_page_count: int | None = None
        super().__init__(path, audit_key=audit_key)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with super()._connect() as connection:
            if self.max_page_count is not None:
                connection.execute(f"PRAGMA max_page_count = {self.max_page_count}")
            yield connection


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _with_digest(payload: dict[str, object]) -> dict[str, object]:
    evidence = dict(payload)
    evidence["evidence_sha256"] = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return evidence


def _validate_scales(scales: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(scales)
    if not normalized:
        raise ValueError("at least one storage measurement scale is required")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > _MAX_SCALE
        for value in normalized
    ):
        raise ValueError(f"storage measurement scales must stay within 1..{_MAX_SCALE}")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("storage measurement scales must be unique and increasing")
    return normalized


def _validate_repeats(repeats: int) -> int:
    if (
        isinstance(repeats, bool)
        or not isinstance(repeats, int)
        or repeats < 1
        or repeats > _MAX_REPEATS
    ):
        raise ValueError(f"repeats must stay within 1..{_MAX_REPEATS}")
    return repeats


def _validate_max_attempts(max_attempts: int) -> int:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
        or max_attempts > _MAX_ATTEMPTS
    ):
        raise ValueError(f"max_attempts must stay within 1..{_MAX_ATTEMPTS}")
    return max_attempts


def _sqlite_footprint(path: Path) -> _Footprint:
    def size(candidate: Path) -> int:
        return candidate.stat().st_size if candidate.exists() else 0

    return _Footprint(
        database_bytes=size(path),
        wal_bytes=size(Path(f"{path}-wal")),
        shm_bytes=size(Path(f"{path}-shm")),
    )


def _single_int(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(query, parameters).fetchone()
    if row is None:
        raise RuntimeError("SQLite storage-pressure query returned no row")
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("SQLite storage-pressure query returned an invalid integer")
    return value


def _page_count(path: Path) -> int:
    connection = sqlite3.connect(path, timeout=30)
    try:
        return _single_int(connection, "PRAGMA page_count")
    finally:
        connection.close()


def _integrity(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=30)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Market Data control-plane SQLite integrity_check failed")
    finally:
        connection.close()


def _settle(path: Path) -> _Footprint:
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Market Data control-plane SQLite settle integrity_check failed")
    finally:
        connection.close()
    return _sqlite_footprint(path)


def _operational_state(path: Path, key: str | None = None) -> _OperationalState:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("operational jobs database integrity_check failed")
        return _OperationalState(
            jobs=_single_int(connection, "SELECT COUNT(*) FROM jobs"),
            events=_single_int(connection, "SELECT COUNT(*) FROM job_events"),
            leases=_single_int(connection, "SELECT COUNT(*) FROM job_leases"),
            confirmations=_single_int(connection, "SELECT COUNT(*) FROM repair_confirmations"),
            idempotency_rows=(
                0
                if key is None
                else _single_int(
                    connection,
                    "SELECT COUNT(*) FROM jobs WHERE idempotency_key=?",
                    (key,),
                )
            ),
        )
    finally:
        connection.close()


def _security_state(path: Path) -> _SecurityState:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("security audit database integrity_check failed")
        row = connection.execute(
            "SELECT generation FROM control_plane_state WHERE singleton=1"
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise RuntimeError("security audit database generation is invalid")
        return _SecurityState(
            generation=row[0],
            audit_records=_single_int(connection, "SELECT COUNT(*) FROM control_plane_audit"),
        )
    finally:
        connection.close()


def _audit(action: str, *, offset: int = 0, pressure: bool = False) -> AuditContext:
    details: dict[str, object] = {"sequence": offset}
    if pressure:
        details.update(
            {
                f"pressure_{index:02d}": "x" * _BOUNDARY_AUDIT_FIELD_BYTES
                for index in range(_BOUNDARY_AUDIT_FIELDS)
            }
        )
    return AuditContext(
        timestamp=_BASE_TIME + timedelta(seconds=offset),
        correlation_id=f"corr-storage-pressure-{offset:06d}",
        direct_peer="local_benchmark",
        principal_id="storage-pressure-admin",
        token_id="storage-pressure-admin",
        action=action,
        outcome="accepted",
        details=details,
    )


def _token_state(generation: int) -> TokenSnapshotState:
    record = ApiTokenRecord.from_plaintext(
        token_id="storage-pressure-admin",
        token=_TOKEN,
        role=ApiRole.ADMIN,
        not_before=_BASE_TIME,
    )
    return TokenSnapshotState(
        current=ApiTokenSnapshot(
            generation=generation,
            created_at=_BASE_TIME + timedelta(seconds=generation),
            records=(record,),
        )
    )


def _boundary_request(index: int) -> Mapping[str, object]:
    return {
        "symbol": "BTCUSDT",
        "sequence": index,
        "pressure": "x" * _BOUNDARY_REQUEST_BYTES,
    }


def _initialize_operational_schema(path: Path) -> None:
    SQLiteJobStore(path)
    SQLiteJobLeaseStore(path)
    SQLiteRepairConfirmationStore(path, token_factory=lambda _size: "r" * 43)


def _operational_growth(path: Path, scale: int) -> tuple[_Footprint, _Footprint]:
    _initialize_operational_schema(path)
    jobs = SQLiteJobStore(path)
    leases = SQLiteJobLeaseStore(path)
    keeper = sqlite3.connect(path, timeout=30)
    identifiers: list[str] = []
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        for index in range(scale):
            created = jobs.create_job(
                operation=JobOperation.DOWNLOAD,
                request={"symbol": "BTCUSDT", "sequence": index},
                idempotency_key=f"storage-growth-{index:06d}",
            )
            identifiers.append(created.job.job_id)
            at = _BASE_TIME + timedelta(minutes=index)
            lease = leases.claim(
                created.job.job_id,
                owner_id="storage-pressure-worker",
                ttl_seconds=30,
                at=at,
            )
            if lease is None:
                raise RuntimeError("storage growth benchmark could not claim a fresh job lease")
            renewed = leases.renew(lease, ttl_seconds=30, at=at + timedelta(seconds=1))
            if renewed is None or not leases.release(renewed):
                raise RuntimeError("storage growth benchmark lease lifecycle failed")
            jobs.transition(created.job.job_id, RunState.CANCELLED)
        live = _sqlite_footprint(path)
    finally:
        keeper.close()

    state = _operational_state(path)
    if state.jobs != scale or state.events != scale * 2 or state.leases != 0:
        raise RuntimeError("operational storage growth durable accounting is inconsistent")
    reopened = SQLiteJobStore(path)
    for job_id in identifiers:
        reopened.get_job(job_id)
        if len(reopened.events_after(job_id)) != 2:
            raise RuntimeError("operational storage growth event identity changed on reopen")
    _integrity(path)
    return live, _settle(path)


def _security_growth(path: Path, scale: int) -> tuple[_Footprint, _Footprint]:
    store = SQLiteControlPlaneStore(path, audit_key=_AUDIT_KEY)
    keeper = sqlite3.connect(path, timeout=30)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        store.bootstrap(_token_state(1), audit=_audit("security.bootstrap"))
        for index in range(scale):
            store.append_audit(_audit("security.storage_pressure", offset=index + 1))
        live = _sqlite_footprint(path)
    finally:
        keeper.close()
    status = store.verify_audit_chain()
    if not status.valid or status.record_count != scale + 1:
        raise RuntimeError("security storage growth audit chain is inconsistent")
    reopened = SQLiteControlPlaneStore(path, audit_key=_AUDIT_KEY)
    if reopened.load_state().current.generation != 1 or reopened.verify_audit_chain() != status:
        raise RuntimeError("security storage growth identity changed on reopen")
    _integrity(path)
    return live, _settle(path)


def _growth_summary(
    samples: dict[int, list[tuple[_Footprint, _Footprint]]],
    *,
    identity_key: str,
) -> dict[str, object]:
    live = [item for group in samples.values() for item, _ in group]
    settled = [item for group in samples.values() for _, item in group]
    repeat_noise = 0
    for group in samples.values():
        totals = [item.total_bytes for item, _ in group]
        repeat_noise = max(repeat_noise, max(totals) - min(totals))
    return {
        "max_live_total_bytes": max(item.total_bytes for item in live),
        "max_live_database_bytes": max(item.database_bytes for item in live),
        "max_wal_bytes": max(item.wal_bytes for item in live),
        "max_shm_bytes": max(item.shm_bytes for item in live),
        "max_settled_database_bytes": max(item.database_bytes for item in settled),
        "repeat_noise_bytes": repeat_noise,
        "integrity_reopen": "PASS",
        identity_key: "PASS",
    }


def measure_control_plane_storage_growth(
    *,
    scales: Sequence[int] = (1, 100, 1000),
    repeats: int = 3,
    work_root: Path | None = None,
) -> dict[str, object]:
    accepted_scales = _validate_scales(scales)
    accepted_repeats = _validate_repeats(repeats)
    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="market-data-control-plane-growth-") as raw:
            return _measure_growth(Path(raw), accepted_scales, accepted_repeats)
    work_root.mkdir(parents=True, exist_ok=True)
    return _measure_growth(work_root, accepted_scales, accepted_repeats)


def _measure_growth(root: Path, scales: tuple[int, ...], repeats: int) -> dict[str, object]:
    operational: dict[int, list[tuple[_Footprint, _Footprint]]] = {}
    security: dict[int, list[tuple[_Footprint, _Footprint]]] = {}
    for scale in scales:
        operational[scale] = []
        security[scale] = []
        for repeat in range(repeats):
            sample = root / f"scale-{scale}-repeat-{repeat}"
            sample.mkdir(parents=True, exist_ok=True)
            operational[scale].append(_operational_growth(sample / "jobs.sqlite3", scale))
            security[scale].append(_security_growth(sample / "security.sqlite3", scale))
    return _with_digest(
        {
            "schema_version": 1,
            "owner": "market_data_control_plane",
            "measurement": "sqlite_growth",
            "scales": list(scales),
            "repeats": repeats,
            "stores": {
                "operational_jobs": _growth_summary(
                    operational,
                    identity_key="job_event_identity",
                )
                | {"lease_lifecycle": "PASS"},
                "security_audit": _growth_summary(
                    security,
                    identity_key="audit_chain",
                )
                | {"token_generation_reopen": "PASS"},
            },
            "physical_high_water_bytes": None,
            "physical_critical_bytes": None,
            "watermark_status": "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY",
            "deletion_authority": "NONE",
        }
    )


def _operational_boundary(path: Path, max_attempts: int) -> dict[str, object]:
    _initialize_operational_schema(path)
    jobs = _BoundedJobStore(path)
    leases = SQLiteJobLeaseStore(path)
    baseline = jobs.create_job(
        operation=JobOperation.DOWNLOAD,
        request={"symbol": "BTCUSDT", "sequence": "baseline"},
        idempotency_key="storage-boundary-baseline",
    )
    baseline_lease = leases.claim(
        baseline.job.job_id,
        owner_id="storage-boundary-worker",
        ttl_seconds=30,
        at=_BASE_TIME,
    )
    if baseline_lease is None or not leases.release(baseline_lease):
        raise RuntimeError("operational boundary baseline lease lifecycle failed")
    jobs.transition(baseline.job.job_id, RunState.CANCELLED)
    jobs.max_page_count = _page_count(path)

    failed_key: str | None = None
    failed_index: int | None = None
    for index in range(1, max_attempts + 1):
        key = f"storage-boundary-{index:03d}"
        before = _operational_state(path, key)
        try:
            jobs.create_job(
                operation=JobOperation.DOWNLOAD,
                request=_boundary_request(index),
                idempotency_key=key,
            )
        except sqlite3.OperationalError as error:
            if "full" not in str(error).lower():
                raise
            after = _operational_state(path, key)
            if after.durable_counts() != before.durable_counts():
                raise RuntimeError("operational SQLITE_FULL changed durable row counts") from error
            if after.idempotency_rows != 0 or after.leases != before.leases:
                raise RuntimeError(
                    "operational SQLITE_FULL leaked job or lease identity"
                ) from error
            failed_key = key
            failed_index = index
            break
    if failed_key is None or failed_index is None:
        raise RuntimeError("operational boundary did not reach SQLITE_FULL within its bound")

    jobs.max_page_count = None
    retry = jobs.create_job(
        operation=JobOperation.DOWNLOAD,
        request=_boundary_request(failed_index),
        idempotency_key=failed_key,
    )
    replay = jobs.create_job(
        operation=JobOperation.DOWNLOAD,
        request=_boundary_request(failed_index),
        idempotency_key=failed_key,
    )
    if retry.reused or not replay.reused or replay.job.job_id != retry.job.job_id:
        raise RuntimeError("operational boundary idempotency recovery failed")
    retry_lease = leases.claim(
        retry.job.job_id,
        owner_id="storage-boundary-retry-worker",
        ttl_seconds=30,
        at=_BASE_TIME + timedelta(hours=1),
    )
    if retry_lease is None or not leases.release(retry_lease):
        raise RuntimeError("operational boundary lease recovery failed")
    _integrity(path)
    SQLiteJobStore(path).get_job(retry.job.job_id)
    final = _operational_state(path, failed_key)
    if final.idempotency_rows != 1 or final.leases != 0:
        raise RuntimeError("operational boundary recovery did not commit exactly one job")
    return {
        "sqlite_full_observed": True,
        "failure_transaction_atomic": True,
        "failed_job_identity_absent": True,
        "failed_lease_identity_absent": True,
        "integrity_reopen": "PASS",
        "same_request_retry": "PASS",
        "idempotent_reuse": "PASS",
    }


def _security_boundary(path: Path, max_attempts: int) -> dict[str, object]:
    store = _BoundedControlPlaneStore(path, audit_key=_AUDIT_KEY)
    store.bootstrap(_token_state(1), audit=_audit("security.bootstrap"))
    store.max_page_count = _page_count(path)

    failed_state: TokenSnapshotState | None = None
    expected_generation: int | None = None
    before_failure: _SecurityState | None = None
    failed_offset = 0
    for index in range(1, max_attempts + 1):
        before = _security_state(path)
        candidate = _token_state(before.generation + 1)
        try:
            store.replace_state(
                candidate,
                expected_generation=before.generation,
                audit=_audit("security.storage_pressure", offset=index, pressure=True),
            )
        except sqlite3.OperationalError as error:
            if "full" not in str(error).lower():
                raise
            after = _security_state(path)
            chain = store.verify_audit_chain()
            if after != before or not chain.valid or chain.record_count != before.audit_records:
                raise RuntimeError(
                    "security SQLITE_FULL changed committed state or audit chain"
                ) from error
            failed_state = candidate
            expected_generation = before.generation
            before_failure = before
            failed_offset = index
            break
    if failed_state is None or expected_generation is None or before_failure is None:
        raise RuntimeError("security boundary did not reach SQLITE_FULL within its bound")

    store.max_page_count = None
    store.replace_state(
        failed_state,
        expected_generation=expected_generation,
        audit=_audit("security.storage_pressure.recovery", offset=failed_offset, pressure=True),
    )
    recovered = _security_state(path)
    if (
        recovered.generation != expected_generation + 1
        or recovered.audit_records != before_failure.audit_records + 1
    ):
        raise RuntimeError("security boundary retry did not commit exactly once")
    try:
        store.replace_state(
            failed_state,
            expected_generation=expected_generation,
            audit=_audit("security.storage_pressure.stale", offset=failed_offset + 1),
        )
    except ControlPlaneStateError:
        compare_and_swap = True
    else:
        raise RuntimeError("security boundary stale generation unexpectedly committed")
    reopened = SQLiteControlPlaneStore(path, audit_key=_AUDIT_KEY)
    chain = reopened.verify_audit_chain()
    if reopened.load_state().current.generation != recovered.generation:
        raise RuntimeError("security boundary reopen changed token generation")
    if not chain.valid or chain.record_count != recovered.audit_records:
        raise RuntimeError("security boundary reopen changed the HMAC audit chain")
    _integrity(path)
    return {
        "sqlite_full_observed": True,
        "failure_transaction_atomic": True,
        "committed_generation_preserved": True,
        "audit_chain_preserved": True,
        "integrity_reopen": "PASS",
        "capacity_restore_retry": "PASS",
        "generation_compare_and_swap": "PASS" if compare_and_swap else "FAIL",
    }


def measure_control_plane_storage_boundary(
    *,
    max_attempts: int = 64,
    work_root: Path | None = None,
) -> dict[str, object]:
    attempts = _validate_max_attempts(max_attempts)
    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="market-data-control-plane-boundary-") as raw:
            return _measure_boundary(Path(raw), attempts)
    work_root.mkdir(parents=True, exist_ok=True)
    return _measure_boundary(work_root, attempts)


def _measure_boundary(root: Path, max_attempts: int) -> dict[str, object]:
    operational = root / "operational"
    security = root / "security"
    operational.mkdir(parents=True, exist_ok=True)
    security.mkdir(parents=True, exist_ok=True)
    return _with_digest(
        {
            "schema_version": 1,
            "owner": "market_data_control_plane",
            "measurement": "sqlite_near_boundary_recovery",
            "boundary_mechanism": "sqlite_max_page_count",
            "max_attempts": max_attempts,
            "proofs": {
                "operational_jobs": _operational_boundary(
                    operational / "jobs.sqlite3",
                    max_attempts,
                ),
                "security_audit": _security_boundary(
                    security / "control-plane.sqlite3",
                    max_attempts,
                ),
            },
            "physical_high_water_bytes": None,
            "physical_critical_bytes": None,
            "watermark_status": "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY",
            "deletion_authority": "NONE",
        }
    )
