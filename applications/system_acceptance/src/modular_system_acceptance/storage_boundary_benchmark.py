from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceMutationResult,
    AcceptanceOwnerError,
    AcceptanceRunSpec,
    AcceptanceStatus,
    TrustedOperator,
)
from .owner import AcceptanceExecutorRegistry, SystemAcceptanceOwner
from .storage import SQLiteAcceptanceStore

_MAX_ATTEMPTS = 256
_ZERO_SHA256 = "0" * 64
_ZERO_GIT_SHA = "0" * 40


@dataclass(frozen=True, slots=True)
class _DurableState:
    runs: int
    mutations: int
    audit: int
    in_progress_mutations: int
    request_rows: int
    request_state: str | None
    request_error_code: str | None

    def counts(self) -> dict[str, int]:
        return {
            "runs": self.runs,
            "mutations": self.mutations,
            "audit": self.audit,
        }


class _DeterministicClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(seconds=1)
        return value


def _spec() -> AcceptanceRunSpec:
    return AcceptanceRunSpec(
        full_rc_sha=_ZERO_GIT_SHA,
        dataset_id="storage-boundary-dataset",
        dataset_digest=_ZERO_SHA256,
        strategy_id="storage-boundary-strategy",
        strategy_digest=_ZERO_SHA256,
        config_digest=_ZERO_SHA256,
    )


def _request(
    request_id: str,
    mutation: AcceptanceMutation,
    *,
    run_id: str | None = None,
) -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id=request_id,
        operator=TrustedOperator("storage-boundary-benchmark"),
        mutation=mutation,
        run_id=run_id,
        spec=_spec() if mutation is AcceptanceMutation.START else None,
    )


def _replacement_request(request: AcceptanceMutationRequest) -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id=f"{request.request_id}-recovery",
        operator=request.operator,
        mutation=request.mutation,
        run_id=request.run_id,
        spec=request.spec,
    )


def _single_int(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(query, parameters).fetchone()
    if row is None:
        raise RuntimeError("SQLite boundary query returned no row")
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("SQLite boundary query returned an invalid integer")
    return value


def _read_only_state(path: Path, *, request_id: str | None = None) -> _DurableState:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError("System Acceptance SQLite integrity_check failed")
        runs = _single_int(connection, "SELECT COUNT(*) FROM acceptance_runs")
        mutations = _single_int(
            connection,
            "SELECT COUNT(*) FROM acceptance_mutations",
        )
        audit = _single_int(connection, "SELECT COUNT(*) FROM acceptance_audit")
        in_progress = _single_int(
            connection,
            "SELECT COUNT(*) FROM acceptance_mutations WHERE state='IN_PROGRESS'",
        )
        request_rows = 0
        request_state: str | None = None
        request_error_code: str | None = None
        if request_id is not None:
            rows = connection.execute(
                "SELECT state,error_code FROM acceptance_mutations WHERE request_id=?",
                (request_id,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError("storage-boundary request identity is not unique")
            request_rows = len(rows)
            if rows:
                raw_state, raw_error = rows[0]
                if not isinstance(raw_state, str):
                    raise RuntimeError("storage-boundary mutation state is invalid")
                if raw_error is not None and not isinstance(raw_error, str):
                    raise RuntimeError("storage-boundary mutation error code is invalid")
                request_state = raw_state
                request_error_code = raw_error
    finally:
        connection.close()
    return _DurableState(
        runs=runs,
        mutations=mutations,
        audit=audit,
        in_progress_mutations=in_progress,
        request_rows=request_rows,
        request_state=request_state,
        request_error_code=request_error_code,
    )


def _pragma_int(store: SQLiteAcceptanceStore, name: str) -> int:
    if name not in {"page_count", "page_size", "max_page_count"}:
        raise ValueError("unsupported SQLite boundary pragma")
    with store.transaction() as connection:
        row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        raise RuntimeError("SQLite boundary pragma returned no row")
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("SQLite boundary pragma returned an invalid value")
    return value


def _set_max_page_count(store: SQLiteAcceptanceStore, value: int) -> int:
    if value < 1:
        raise ValueError("SQLite max_page_count must be positive")
    with store.transaction() as connection:
        row = connection.execute(f"PRAGMA max_page_count={value}").fetchone()
    if row is None:
        raise RuntimeError("SQLite max_page_count update returned no row")
    applied = row[0]
    if isinstance(applied, bool) or not isinstance(applied, int):
        raise RuntimeError("SQLite max_page_count update returned an invalid value")
    return applied


def _owner_mutate(
    owner: SystemAcceptanceOwner,
    request: AcceptanceMutationRequest,
) -> AcceptanceMutationResult:
    try:
        return owner.mutate(request)
    except sqlite3.Error:
        raise RuntimeError("System Acceptance owner leaked a raw SQLite storage error") from None


def _validate_max_attempts(max_attempts: int) -> int:
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= _MAX_ATTEMPTS
    ):
        raise ValueError(f"max_attempts must stay within 1..{_MAX_ATTEMPTS}")
    return max_attempts


def measure_storage_boundary_recovery(
    *,
    max_attempts: int = 64,
    work_root: Path | None = None,
) -> dict[str, object]:
    bounded_attempts = _validate_max_attempts(max_attempts)
    if work_root is None:
        with tempfile.TemporaryDirectory(prefix="system-acceptance-storage-boundary-") as raw_root:
            return _measure(Path(raw_root), bounded_attempts)
    work_root.mkdir(parents=True, exist_ok=True)
    return _measure(work_root, bounded_attempts)


def _measure(root: Path, max_attempts: int) -> dict[str, object]:
    database = root / "acceptance.sqlite3"
    clock = _DeterministicClock()
    store = SQLiteAcceptanceStore(database)
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({}),
        instance_id="storage-boundary-benchmark",
        clock=clock,
    )

    initial_page_count = _pragma_int(store, "page_count")
    page_size_bytes = _pragma_int(store, "page_size")
    original_max_page_count = _pragma_int(store, "max_page_count")
    constrained_target = initial_page_count + 1
    if constrained_target >= original_max_page_count:
        store.close()
        raise RuntimeError("SQLite boundary benchmark cannot reduce max_page_count safely")
    constrained_max_page_count = _set_max_page_count(store, constrained_target)
    if constrained_max_page_count != constrained_target:
        store.close()
        raise RuntimeError("SQLite boundary benchmark did not apply the reviewed page ceiling")

    successful_terminal_runs = 0
    failed_request: AcceptanceMutationRequest | None = None
    failed_error_message: str | None = None
    state_after_failure: _DurableState | None = None
    recovery_request: AcceptanceMutationRequest | None = None
    recovery_state: _DurableState | None = None
    final_state: _DurableState | None = None
    recovery_mode: str | None = None
    durable_failure_replayed = False
    terminal_status: AcceptanceStatus | None = None

    try:
        for index in range(1, max_attempts + 1):
            start_request = _request(
                f"storage-boundary-start-{index:03d}",
                AcceptanceMutation.START,
            )
            try:
                started = _owner_mutate(owner, start_request)
            except AcceptanceOwnerError as exc:
                if exc.code is not AcceptanceErrorCode.STORAGE_FAILURE:
                    raise RuntimeError(
                        "storage boundary benchmark observed an unexpected owner error"
                    ) from None
                failed_request = start_request
                failed_error_message = str(exc)
                break

            cancel_request = _request(
                f"storage-boundary-cancel-{index:03d}",
                AcceptanceMutation.CANCEL,
                run_id=started.acceptance_run_id,
            )
            try:
                terminal = _owner_mutate(owner, cancel_request)
            except AcceptanceOwnerError as exc:
                if exc.code is not AcceptanceErrorCode.STORAGE_FAILURE:
                    raise RuntimeError(
                        "storage boundary benchmark observed an unexpected owner error"
                    ) from None
                failed_request = cancel_request
                failed_error_message = str(exc)
                break
            if terminal.status is not AcceptanceStatus.CANCELLED:
                raise RuntimeError(
                    "storage boundary benchmark did not terminalize deterministically"
                )
            successful_terminal_runs += 1

        if failed_request is None or failed_error_message is None:
            raise RuntimeError(
                "storage boundary benchmark did not reach SQLITE_FULL within its bound"
            )
        accepted_failed_request = failed_request

        state_after_failure = _read_only_state(
            database,
            request_id=accepted_failed_request.request_id,
        )
        if state_after_failure.in_progress_mutations != 0:
            raise RuntimeError("storage-boundary failure left an in-progress mutation")
        if state_after_failure.request_rows not in {0, 1}:
            raise RuntimeError("storage-boundary failure produced invalid request accounting")

        failure_persisted = state_after_failure.request_rows == 1
        if failure_persisted:
            if (
                state_after_failure.request_state != "FAILED"
                or state_after_failure.request_error_code
                != AcceptanceErrorCode.STORAGE_FAILURE.value
            ):
                raise RuntimeError(
                    "durable storage-boundary failure did not preserve FAILED/STORAGE_FAILURE"
                )
        elif (
            state_after_failure.request_state is not None
            or state_after_failure.request_error_code is not None
        ):
            raise RuntimeError("rolled-back storage-boundary request left partial metadata")

        expected_runs_after_failure = successful_terminal_runs + (
            1 if accepted_failed_request.mutation is AcceptanceMutation.CANCEL else 0
        )
        expected_mutations_after_failure = (
            successful_terminal_runs * 2
            + (1 if accepted_failed_request.mutation is AcceptanceMutation.CANCEL else 0)
            + (1 if failure_persisted else 0)
        )
        expected_audit_after_failure = expected_mutations_after_failure
        if state_after_failure.counts() != {
            "runs": expected_runs_after_failure,
            "mutations": expected_mutations_after_failure,
            "audit": expected_audit_after_failure,
        }:
            raise RuntimeError("storage-boundary failure was not transactionally atomic")

        restored_max_page_count = _set_max_page_count(store, original_max_page_count)
        if restored_max_page_count != original_max_page_count:
            raise RuntimeError("SQLite boundary benchmark failed to restore test capacity")

        replay_audit_increment = 0
        if failure_persisted:
            try:
                _owner_mutate(owner, accepted_failed_request)
            except AcceptanceOwnerError as exc:
                if exc.code is not AcceptanceErrorCode.STORAGE_FAILURE:
                    raise RuntimeError(
                        "durable failed request replay changed the owner error contract"
                    ) from None
            else:
                raise RuntimeError("durable failed request unexpectedly succeeded on replay")
            durable_failure_replayed = True
            replay_state = _read_only_state(
                database,
                request_id=accepted_failed_request.request_id,
            )
            if (
                replay_state.request_rows != 1
                or replay_state.request_state != "FAILED"
                or replay_state.request_error_code != AcceptanceErrorCode.STORAGE_FAILURE.value
                or replay_state.runs != state_after_failure.runs
                or replay_state.mutations != state_after_failure.mutations
                or replay_state.audit != state_after_failure.audit + 1
            ):
                raise RuntimeError("durable failed request replay was not idempotent")
            replay_audit_increment = 1
            recovery_request = _replacement_request(accepted_failed_request)
            recovery_mode = "NEW_REQUEST_AFTER_DURABLE_FAILURE"
        else:
            recovery_request = accepted_failed_request
            recovery_mode = "SAME_REQUEST_RETRY"

        retry_result = _owner_mutate(owner, recovery_request)
        if recovery_request.mutation is AcceptanceMutation.START:
            recovery_cancel = _request(
                "storage-boundary-recovery-cancel",
                AcceptanceMutation.CANCEL,
                run_id=retry_result.acceptance_run_id,
            )
            terminal = _owner_mutate(owner, recovery_cancel)
        else:
            terminal = retry_result
        if terminal.status is not AcceptanceStatus.CANCELLED:
            raise RuntimeError(
                "storage-boundary recovery did not reach the expected terminal state"
            )
        terminal_status = terminal.status

        run_delta = 1 if accepted_failed_request.mutation is AcceptanceMutation.START else 0
        mutation_delta = 2 if accepted_failed_request.mutation is AcceptanceMutation.START else 1
        audit_delta = mutation_delta + replay_audit_increment
        expected_final_counts = {
            "runs": state_after_failure.runs + run_delta,
            "mutations": state_after_failure.mutations + mutation_delta,
            "audit": state_after_failure.audit + audit_delta,
        }
        final_state = _read_only_state(
            database,
            request_id=accepted_failed_request.request_id,
        )
        recovery_state = _read_only_state(
            database,
            request_id=recovery_request.request_id,
        )
        if final_state.counts() != expected_final_counts:
            raise RuntimeError("storage-boundary recovery durable row accounting is inconsistent")
        if final_state.in_progress_mutations != 0:
            raise RuntimeError("storage-boundary recovery left an in-progress mutation")
        if recovery_state.request_rows != 1 or recovery_state.request_state != "SUCCEEDED":
            raise RuntimeError("storage-boundary recovery request did not commit exactly once")
        if failure_persisted:
            if (
                final_state.request_state != "FAILED"
                or final_state.request_error_code != AcceptanceErrorCode.STORAGE_FAILURE.value
            ):
                raise RuntimeError("durable failed request identity was overwritten by recovery")
        elif final_state.request_state != "SUCCEEDED":
            raise RuntimeError("rolled-back failed request did not succeed after capacity restore")
    finally:
        store.close()

    if (
        failed_request is None
        or failed_error_message is None
        or state_after_failure is None
        or recovery_request is None
        or recovery_state is None
        or final_state is None
        or recovery_mode is None
        or terminal_status is None
    ):
        raise RuntimeError("storage-boundary benchmark ended without complete evidence")

    reopened = SQLiteAcceptanceStore(database)
    try:
        latest = reopened.latest_run()
        if latest is None or latest.snapshot.status is not AcceptanceStatus.CANCELLED:
            raise RuntimeError("storage-boundary reopen did not preserve terminal owner state")
        reopened.verify_storage_bound()
    finally:
        reopened.close()
    reopened_state = _read_only_state(database, request_id=recovery_request.request_id)
    if reopened_state.counts() != final_state.counts():
        raise RuntimeError("storage-boundary reopen changed durable row accounting")

    failure_persisted = state_after_failure.request_rows == 1
    return {
        "schema_version": 1,
        "owner": "system_acceptance",
        "measurement": "sqlite_near_boundary_transaction_recovery",
        "boundary_mechanism": "sqlite_max_page_count",
        "max_attempts": max_attempts,
        "initial_page_count": initial_page_count,
        "constrained_max_page_count": constrained_max_page_count,
        "page_size_bytes": page_size_bytes,
        "successful_terminal_runs_before_full": successful_terminal_runs,
        "physical_high_water_bytes": None,
        "physical_critical_bytes": None,
        "watermark_status": "UNKNOWN_NEAR_BOUNDARY_PROOF_ONLY",
        "deletion_authority": "NONE",
        "failure": {
            "code": AcceptanceErrorCode.STORAGE_FAILURE.value,
            "mutation": failed_request.mutation.value,
            "request_id": failed_request.request_id,
            "sanitized_message": failed_error_message,
            "failed_request_persisted": failure_persisted,
            "durable_state": state_after_failure.request_state,
            "durable_error_code": state_after_failure.request_error_code,
            "in_progress_mutations_after_failure": state_after_failure.in_progress_mutations,
            "integrity_check_after_failure": "ok",
            "row_counts_after_failure": state_after_failure.counts(),
        },
        "after_capacity_restore": {
            "recovery_mode": recovery_mode,
            "same_request_retry_succeeded": not failure_persisted,
            "durable_failure_replayed": durable_failure_replayed,
            "recovery_request_committed_once": recovery_state.request_rows == 1,
            "terminal_status": terminal_status.value,
            "integrity_check": "ok",
            "reopen_status": AcceptanceStatus.CANCELLED.value,
            "failed_request_final_state": final_state.request_state,
            "recovery_request_final_state": recovery_state.request_state,
            "in_progress_mutations": reopened_state.in_progress_mutations,
            "row_counts": reopened_state.counts(),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prove System Acceptance SQLite near-boundary rollback/recovery without deriving "
            "or authorizing a physical storage watermark."
        )
    )
    parser.add_argument("--max-attempts", type=int, default=64)
    args = parser.parse_args(argv)
    try:
        result = measure_storage_boundary_recovery(max_attempts=args.max_attempts)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
