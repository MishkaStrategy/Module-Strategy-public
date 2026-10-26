from __future__ import annotations

import argparse
import json
import sqlite3
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import (
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceRunSpec,
    AcceptanceStatus,
    TrustedOperator,
)
from .owner import AcceptanceExecutorRegistry, SystemAcceptanceOwner
from .storage import SQLiteAcceptanceStore

_MAX_SCALE = 10_000
_MAX_REPEATS = 10
_ZERO_SHA256 = "0" * 64
_ZERO_GIT_SHA = "0" * 40


@dataclass(frozen=True, slots=True)
class StorageFootprint:
    database_bytes: int
    wal_bytes: int
    shm_bytes: int
    total_bytes: int


class _DeterministicClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


def _regular_file_bytes(path: Path) -> int:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("storage benchmark encountered a non-regular SQLite component")
    if metadata.st_size < 0:
        raise RuntimeError("storage benchmark encountered an invalid SQLite component size")
    return int(metadata.st_size)


def _footprint(path: Path) -> StorageFootprint:
    database_bytes = _regular_file_bytes(path)
    wal_bytes = _regular_file_bytes(Path(f"{path}-wal"))
    shm_bytes = _regular_file_bytes(Path(f"{path}-shm"))
    return StorageFootprint(
        database_bytes=database_bytes,
        wal_bytes=wal_bytes,
        shm_bytes=shm_bytes,
        total_bytes=database_bytes + wal_bytes + shm_bytes,
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"acceptance_runs", "acceptance_mutations", "acceptance_audit"}:
        raise ValueError("unsupported storage benchmark table")
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    if row is None:
        raise RuntimeError("storage benchmark row count query returned no result")
    return int(row[0])


def _read_only_integrity_and_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row != ("ok",):
            raise RuntimeError("System Acceptance SQLite integrity_check failed")
        runs = _count(connection, "acceptance_runs")
        mutations = _count(connection, "acceptance_mutations")
        audit = _count(connection, "acceptance_audit")
    finally:
        connection.close()
    return {"runs": runs, "mutations": mutations, "audit": audit}


def _spec() -> AcceptanceRunSpec:
    return AcceptanceRunSpec(
        full_rc_sha=_ZERO_GIT_SHA,
        dataset_id="storage-benchmark-dataset",
        dataset_digest=_ZERO_SHA256,
        strategy_id="storage-benchmark-strategy",
        strategy_digest=_ZERO_SHA256,
        config_digest=_ZERO_SHA256,
    )


def _apply_run(owner: SystemAcceptanceOwner, index: int) -> None:
    operator = TrustedOperator("storage-benchmark")
    started = owner.mutate(
        AcceptanceMutationRequest(
            request_id=f"storage-benchmark-start-{index:05d}",
            operator=operator,
            mutation=AcceptanceMutation.START,
            spec=_spec(),
        )
    )
    cancelled = owner.mutate(
        AcceptanceMutationRequest(
            request_id=f"storage-benchmark-cancel-{index:05d}",
            operator=operator,
            mutation=AcceptanceMutation.CANCEL,
            run_id=started.acceptance_run_id,
        )
    )
    if cancelled.status is not AcceptanceStatus.CANCELLED:
        raise RuntimeError("storage benchmark owner workload did not terminate deterministically")


def _validate_inputs(scales: Sequence[int], repeats: int) -> tuple[int, ...]:
    normalized = tuple(int(value) for value in scales)
    if not normalized:
        raise ValueError("at least one storage benchmark scale is required")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("storage benchmark scales must be strictly increasing and unique")
    if normalized[0] < 1 or normalized[-1] > _MAX_SCALE:
        raise ValueError(f"storage benchmark scales must stay within 1..{_MAX_SCALE}")
    if isinstance(repeats, bool) or not 1 <= repeats <= _MAX_REPEATS:
        raise ValueError(f"storage benchmark repeats must stay within 1..{_MAX_REPEATS}")
    return normalized


def _measure_repeat(
    root: Path,
    scales: tuple[int, ...],
    repeat: int,
) -> tuple[dict[str, object], int]:
    repeat_root = root / f"repeat-{repeat:02d}"
    repeat_root.mkdir(parents=True, exist_ok=False)
    database = repeat_root / "acceptance.sqlite3"
    store = SQLiteAcceptanceStore(database)
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({}),
        instance_id="storage-benchmark",
        clock=_DeterministicClock(),
    )
    initial = _footprint(database)
    checkpoints: list[dict[str, object]] = []
    completed = 0
    final_live_total = initial.total_bytes
    try:
        for scale in scales:
            for index in range(completed + 1, scale + 1):
                _apply_run(owner, index)
            completed = scale
            store.verify_storage_bound()
            footprint = _footprint(database)
            final_live_total = footprint.total_bytes
            checkpoints.append(
                {
                    "runs": scale,
                    "footprint": asdict(footprint),
                    "growth_bytes_from_initialized_store": footprint.total_bytes
                    - initial.total_bytes,
                    "growth_bytes_per_run": (footprint.total_bytes - initial.total_bytes) / scale,
                }
            )
    finally:
        store.close()

    settled = _footprint(database)
    counts = _read_only_integrity_and_counts(database)
    expected_runs = scales[-1]
    if counts != {
        "runs": expected_runs,
        "mutations": expected_runs * 2,
        "audit": expected_runs * 2,
    }:
        raise RuntimeError("storage benchmark durable row accounting is inconsistent")

    reopened = SQLiteAcceptanceStore(database)
    try:
        reopened.verify_storage_bound()
        latest = reopened.latest_run()
        if latest is None or latest.snapshot.status is not AcceptanceStatus.CANCELLED:
            raise RuntimeError("storage benchmark reopen did not recover the latest durable run")
        reopened_live = _footprint(database)
    finally:
        reopened.close()
    reopened_settled = _footprint(database)

    return (
        {
            "repeat": repeat,
            "initialized_footprint": asdict(initial),
            "checkpoints": checkpoints,
            "settled_footprint": asdict(settled),
            "reopened_live_footprint": asdict(reopened_live),
            "reopened_settled_footprint": asdict(reopened_settled),
            "row_counts": counts,
            "integrity_check": "ok",
            "reopen_status": AcceptanceStatus.CANCELLED.value,
        },
        final_live_total,
    )


def _measure_recovery_smoke(root: Path) -> dict[str, object]:
    recovery_root = root / "recovery-smoke"
    recovery_root.mkdir(parents=True, exist_ok=False)
    database = recovery_root / "acceptance.sqlite3"
    clock = _DeterministicClock()
    store = SQLiteAcceptanceStore(database)
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({}),
        instance_id="storage-benchmark-before-restart",
        clock=clock,
    )
    started = owner.mutate(
        AcceptanceMutationRequest(
            request_id="storage-benchmark-recovery-start",
            operator=TrustedOperator("storage-benchmark"),
            mutation=AcceptanceMutation.START,
            spec=_spec(),
        )
    )
    if started.status is not AcceptanceStatus.QUEUED:
        raise RuntimeError("storage benchmark recovery smoke did not create an active run")
    live_before_restart = _footprint(database)
    store.close()
    settled_before_restart = _footprint(database)

    reopened_store = SQLiteAcceptanceStore(database)
    reopened_owner = SystemAcceptanceOwner(
        reopened_store,
        AcceptanceExecutorRegistry({}),
        instance_id="storage-benchmark-after-restart",
        clock=clock,
    )
    live_before_recovery = _footprint(database)
    try:
        recovered = reopened_owner.recover_after_restart()
        if recovered is None or recovered.status is not AcceptanceStatus.RECOVERING:
            raise RuntimeError("storage benchmark owner restart did not enter recovery")
        live_after_recovery = _footprint(database)
        cancelled = reopened_owner.mutate(
            AcceptanceMutationRequest(
                request_id="storage-benchmark-recovery-cancel",
                operator=TrustedOperator("storage-benchmark"),
                mutation=AcceptanceMutation.CANCEL,
                run_id=started.acceptance_run_id,
            )
        )
        if cancelled.status is not AcceptanceStatus.CANCELLED:
            raise RuntimeError("storage benchmark recovery smoke did not terminate safely")
        live_after_terminalization = _footprint(database)
        reopened_store.verify_storage_bound()
    finally:
        reopened_store.close()

    settled_after_recovery = _footprint(database)
    counts = _read_only_integrity_and_counts(database)
    if counts != {"runs": 1, "mutations": 2, "audit": 2}:
        raise RuntimeError("storage benchmark recovery row accounting is inconsistent")
    live_peak = max(
        live_before_recovery.total_bytes,
        live_after_recovery.total_bytes,
        live_after_terminalization.total_bytes,
    )
    return {
        "started_status": AcceptanceStatus.QUEUED.value,
        "recovered_status": AcceptanceStatus.RECOVERING.value,
        "terminal_status": AcceptanceStatus.CANCELLED.value,
        "live_before_restart_footprint": asdict(live_before_restart),
        "settled_before_restart_footprint": asdict(settled_before_restart),
        "live_before_recovery_footprint": asdict(live_before_recovery),
        "live_after_recovery_footprint": asdict(live_after_recovery),
        "live_after_terminalization_footprint": asdict(live_after_terminalization),
        "settled_after_recovery_footprint": asdict(settled_after_recovery),
        "measured_recovery_increment_bytes": max(
            0,
            live_after_recovery.total_bytes - live_before_recovery.total_bytes,
        ),
        "measured_recovery_and_terminalization_peak_increment_bytes": max(
            0,
            live_peak - live_before_recovery.total_bytes,
        ),
        "row_counts": counts,
        "integrity_check": "ok",
    }


def measure_storage_growth(
    *,
    scales: Sequence[int] = (1, 100, 1_000),
    repeats: int = 3,
    work_root: Path | None = None,
) -> dict[str, object]:
    normalized = _validate_inputs(scales, repeats)
    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="system-acceptance-storage-growth-") as raw_root:
            return _measure(Path(raw_root), normalized, repeats)
    work_root.mkdir(parents=True, exist_ok=True)
    return _measure(work_root, normalized, repeats)


def _measure(root: Path, scales: tuple[int, ...], repeats: int) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    final_live_totals: list[int] = []
    for repeat in range(1, repeats + 1):
        case, final_live_total = _measure_repeat(root, scales, repeat)
        cases.append(case)
        final_live_totals.append(final_live_total)
    return {
        "schema_version": 1,
        "owner": "system_acceptance",
        "measurement": "sqlite_owner_storage_growth",
        "workload": "public_owner_start_then_cancel_plus_restart_recovery",
        "scales": list(scales),
        "repeats": repeats,
        "physical_high_water_bytes": None,
        "physical_critical_bytes": None,
        "watermark_status": "UNKNOWN_MEASUREMENT_ONLY",
        "deletion_authority": "NONE",
        "sqlite_version": sqlite3.sqlite_version,
        "cases": cases,
        "recovery_smoke": _measure_recovery_smoke(root),
        "final_live_total_bytes": {
            "min": min(final_live_totals),
            "max": max(final_live_totals),
        },
    }


def _parse_scales(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scales must be comma-separated integers") from exc
    try:
        return _validate_inputs(values, 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure System Acceptance SQLite owner growth without deriving or authorizing "
            "storage watermarks."
        )
    )
    parser.add_argument("--scales", type=_parse_scales, default=(1, 100, 1_000))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        result = measure_storage_growth(scales=args.scales, repeats=args.repeats)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
