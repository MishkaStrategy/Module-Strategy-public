from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from modular_core import EngineConfig, InstrumentSpec, Journal, SQLiteCoreLifecycleCommitPort
from modular_core.execution import BrokerSimulator
from modular_core.lifecycle import (
    CoreAtomicCommitOutcomeUnknown,
    CoreAtomicCommitRejected,
    CoreAuthorityReference,
    CoreCommitStatus,
    CoreEffectCommitReceipt,
    CoreEffectValidation,
    CoreFenceTransition,
    CoreFenceTransitionReceipt,
    CoreFenceValidation,
    CoreLifecycleAction,
    CoreLifecycleCommitCoordinator,
    CoreLifecycleCommitResult,
    CoreLifecycleEffect,
    CoreLifecycleExpectedGeneration,
    CoreStagedEffectCommit,
    CoreStagedTransitionCommit,
    CoreWriterIdentity,
)

from modular_trading_runner.strategy_lifecycle import (
    LifecycleCommandError,
    LifecycleGenerationConflict,
    SQLiteStrategyLifecycleStore,
    StrategyLifecycleCommand,
    StrategyLifecycleOwner,
    StrategyLifecycleService,
    StrategyLifecycleState,
)

_DIGEST = "a" * 64


def owner(path, *, approved=True):
    return StrategyLifecycleOwner(
        SQLiteStrategyLifecycleStore(path),
        audit_approved_for_paper=lambda _package_id, _digest: approved,
        clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
    )


def _core_transition_builder(*, request_id, instance, **_lifecycle_generations):
    payload = b'{"action":"SUSPEND"}'
    return CoreFenceTransition(
        request_id,
        CoreLifecycleExpectedGeneration(
            instance.instance_id, instance.incarnation_id, instance.package_id,
            instance.package_digest, 41,
        ),
        CoreWriterIdentity("core-owner", 3),
        42,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(payload).hexdigest(),
        payload,
        (CoreAuthorityReference("paper-admission", "admission-1", _DIGEST),),
    )


class _LifecycleCoreCoordinator:
    def __init__(self, mode="committed"):
        self.mode = mode
        self.requests = []
        self.receipt = None

    def commit_transition(self, request):
        self.requests.append(request)
        if self.mode == "denied":
            return CoreLifecycleCommitResult(CoreCommitStatus.DENIED, None, False, "denied")
        if self.mode == "unresolved":
            return CoreLifecycleCommitResult(CoreCommitStatus.UNRESOLVED, None, False, "unknown")
        claim = request.lifecycle
        self.receipt = CoreFenceTransitionReceipt(
            request.transition_id,
            "wrong-instance" if self.mode == "mismatch" else claim.instance_id,
            claim.incarnation_id,
            claim.package_id,
            claim.package_digest,
            request.writer,
            claim.expected_generation,
            request.resulting_generation,
            request.action,
            request.canonical_payload_digest,
            request.authority_refs,
            99,
            datetime(2026, 10, 25, tzinfo=UTC),
        )
        return CoreLifecycleCommitResult(
            CoreCommitStatus.COMMITTED, self.receipt, len(self.requests) > 1, "committed"
        )


def _configured_owner(path, coordinator):
    return StrategyLifecycleOwner(
        SQLiteStrategyLifecycleStore(path),
        audit_approved_for_paper=lambda _package_id, _digest: True,
        clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        core_request_builder=_core_transition_builder,
        core_coordinator=coordinator,
    )


def create_v1_database(path, *, state="PENDING_AUTHORIZATION"):
    connection = sqlite3.connect(path)
    connection.executescript(
        "BEGIN;"
        "CREATE TABLE strategy_lifecycle_schema(version INTEGER PRIMARY KEY);"
        "INSERT INTO strategy_lifecycle_schema VALUES(1);"
        "CREATE TABLE strategy_instances("
        "instance_id TEXT PRIMARY KEY, incarnation_id TEXT NOT NULL UNIQUE,"
        "package_id TEXT NOT NULL, package_digest TEXT NOT NULL, runner_id TEXT NOT NULL,"
        "state TEXT NOT NULL, governance_running INTEGER NOT NULL,"
        "observed_runner_state TEXT NOT NULL,"
        "supersedes_instance_id TEXT REFERENCES strategy_instances(instance_id),"
        "cancellation_status TEXT NOT NULL, reconciliation_status TEXT NOT NULL,"
        "admission_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
        "CHECK(state<>'RUNNING'), CHECK(governance_running=0), CHECK(admission_id IS NULL));"
        "CREATE INDEX strategy_instances_package ON strategy_instances(package_id,package_digest);"
        "CREATE INDEX strategy_instances_runner ON strategy_instances(runner_id);"
        "CREATE TRIGGER strategy_instance_binding_immutable "
        "BEFORE UPDATE OF instance_id,incarnation_id,package_id,package_digest,runner_id "
        "ON strategy_instances BEGIN SELECT RAISE(ABORT, "
        "'strategy instance identity and package binding are immutable'); END;"
        "CREATE TRIGGER strategy_instance_authorization_insert_guard "
        "BEFORE INSERT ON strategy_instances WHEN NEW.state='RUNNING' "
        "OR NEW.governance_running<>0 OR NEW.admission_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'strategy authorization source unavailable'); END;"
        "CREATE TRIGGER strategy_instance_authorization_update_guard "
        "BEFORE UPDATE OF state,governance_running,admission_id ON strategy_instances "
        "WHEN NEW.state='RUNNING' OR NEW.governance_running<>0 "
        "OR NEW.admission_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'strategy authorization source unavailable'); END;"
        "CREATE TABLE strategy_lifecycle_commands("
        "request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,"
        "result_json TEXT NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE strategy_lifecycle_audit("
        "event_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
        "request_id TEXT NOT NULL, command TEXT NOT NULL, instance_id TEXT NOT NULL,"
        "actor TEXT NOT NULL, result TEXT NOT NULL, previous_state TEXT NOT NULL,"
        "new_state TEXT NOT NULL);"
        "INSERT INTO strategy_instances VALUES("
        "'si_old','inc_old','pkg','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',"
        "'runner','" + state + "',0,'UNKNOWN',NULL,'UNAVAILABLE','UNRESOLVED',NULL,'t0','t0');"
        "COMMIT;"
    )
    result = {
        "instance_id": "si_old",
        "incarnation_id": "inc_old",
        "package_id": "pkg",
        "package_digest": "a" * 64,
        "runner_id": "runner",
        "state": state,
        "governance_running": False,
        "observed_runner_state": "UNKNOWN",
        "supersedes_instance_id": None,
        "cancellation_status": "UNAVAILABLE",
        "reconciliation_status": "UNRESOLVED",
        "admission_id": None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(["si_old", "SUSPEND", "operator"], separators=(",", ":")).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO strategy_lifecycle_commands VALUES(?,?,?,?)",
        ("legacy-suspend", fingerprint, json.dumps(result), "t1"),
    )
    connection.commit()
    connection.close()


def test_suspend_persists_identity_and_recovery_keeps_instance(tmp_path):
    db_path = tmp_path / "lifecycle.sqlite"
    lifecycle = owner(db_path)
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    suspended = lifecycle.command(
        request_id="suspend-1",
        instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND,
        expected_generation=0,
        actor="operator-1",
    ).instance
    assert suspended.state is StrategyLifecycleState.SUSPEND_PENDING
    assert suspended.lifecycle_generation == 1
    assert suspended.core_fence_status == "PENDING_CORE_COMMIT"
    assert suspended.governance_running is False
    assert suspended.cancellation_status == "UNAVAILABLE"
    assert suspended.reconciliation_status == "UNRESOLVED"
    lifecycle.store.close()

    recovered = owner(db_path).get_instance(instance.instance_id)
    assert (recovered.instance_id, recovered.incarnation_id) == (
        instance.instance_id,
        instance.incarnation_id,
    )
    assert recovered.state is StrategyLifecycleState.SUSPEND_PENDING
    assert recovered.lifecycle_generation == 1
    assert recovered.core_fence_status == "PENDING_CORE_COMMIT"


def test_core_receipt_confirms_only_exact_saved_transition(tmp_path):
    coordinator = _LifecycleCoreCoordinator()
    lifecycle = _configured_owner(tmp_path / "confirmed.sqlite", coordinator)
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    result = lifecycle.command(
        request_id="confirmed-suspend", instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
    )
    assert result.instance.state is StrategyLifecycleState.SUSPENDED
    assert result.enforcement_status == "ENFORCED"
    assert type(result.core_receipt) is CoreFenceTransitionReceipt
    assert result.core_receipt.matches_request(coordinator.requests[0])
    assert coordinator.requests[0].lifecycle.expected_generation == 41
    assert result.expected_lifecycle_generation == 0
    assert result.resulting_lifecycle_generation == 1
    outbox = lifecycle.store._db.execute(
        "SELECT status,expected_lifecycle_generation,resulting_lifecycle_generation,receipt_sha256 "
        "FROM strategy_lifecycle_core_outbox WHERE request_id='confirmed-suspend'"
    ).fetchone()
    assert outbox[:3] == ("ENFORCED", 0, 1)
    assert outbox[3] == hashlib.sha256(
        lifecycle.store._db.execute(
            "SELECT receipt_json FROM strategy_lifecycle_core_outbox WHERE request_id='confirmed-suspend'"
        ).fetchone()[0].encode("utf-8")
    ).hexdigest()
    saved = json.loads(lifecycle.store._db.execute(
        "SELECT result_json FROM strategy_lifecycle_commands WHERE request_id='confirmed-suspend'"
    ).fetchone()[0])
    assert saved["enforcement_status"] == "ENFORCED"
    assert saved["core_receipt"]["transition_id"] == "confirmed-suspend"


def test_core_commit_before_lifecycle_ack_recovers_exact_outbox_request(tmp_path, monkeypatch):
    db_path = tmp_path / "lost-lifecycle-ack.sqlite"
    coordinator = _LifecycleCoreCoordinator()
    first = _configured_owner(db_path, coordinator)
    instance = first.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    def lose_ack(*_args, **_kwargs):
        raise OSError("injected lifecycle acknowledgement loss")

    monkeypatch.setattr(first, "_apply_core_confirmation", lose_ack)
    with pytest.raises(OSError, match="acknowledgement loss"):
        first.command(
            request_id="recover-suspend", instance_id=instance.instance_id,
            command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
        )
    saved_request = first.store._db.execute(
        "SELECT core_request_json FROM strategy_lifecycle_core_outbox WHERE request_id=?",
        ("recover-suspend",),
    ).fetchone()[0]
    first.store.close()

    recovered = _configured_owner(db_path, coordinator)
    monkeypatch.setattr(recovered, "_core_request_builder", lambda **_kwargs: pytest.fail(
        "recovery must replay the saved typed request"
    ))
    replay = recovered.reconcile_suspend("recover-suspend")
    assert replay.instance.state is StrategyLifecycleState.SUSPENDED
    assert replay.core_receipt == coordinator.receipt
    assert len(coordinator.requests) == 2
    assert coordinator.requests[0] == coordinator.requests[1]
    assert recovered.store._db.execute(
        "SELECT core_request_json FROM strategy_lifecycle_core_outbox WHERE request_id=?",
        ("recover-suspend",),
    ).fetchone()[0] == saved_request
def test_mismatched_core_receipt_keeps_lifecycle_pending(tmp_path):
    coordinator = _LifecycleCoreCoordinator(mode="mismatch")
    lifecycle = _configured_owner(tmp_path / "mismatch.sqlite", coordinator)
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    result = lifecycle.command(
        request_id="mismatch-suspend", instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
    )
    assert result.instance.state is StrategyLifecycleState.SUSPEND_PENDING
    assert result.enforcement_status == "PENDING_CORE_COMMIT"
    assert result.core_receipt is None
    assert lifecycle.store._db.execute(
        "SELECT status,receipt_json FROM strategy_lifecycle_core_outbox WHERE request_id=?",
        ("mismatch-suspend",),
    ).fetchone() == ("PENDING", None)


@pytest.mark.parametrize("mode", ["denied", "unresolved"])
def test_denied_or_unresolved_core_transition_stays_pending(tmp_path, mode):
    coordinator = _LifecycleCoreCoordinator(mode=mode)
    lifecycle = _configured_owner(tmp_path / f"{mode}.sqlite", coordinator)
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    result = lifecycle.command(
        request_id=f"{mode}-suspend", instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
    )
    assert result.instance.state is StrategyLifecycleState.SUSPEND_PENDING
    assert result.enforcement_status == "PENDING_CORE_COMMIT"
    assert result.core_receipt is None
    with pytest.raises(LifecycleCommandError, match="pending Core fence"):
        lifecycle.command(
            request_id=f"{mode}-resume", instance_id=instance.instance_id,
            command=StrategyLifecycleCommand.RESUME, expected_generation=1, actor="operator",
        )


def test_duplicate_suspend_replays_lifecycle_and_core_receipts_once(tmp_path):
    coordinator = _LifecycleCoreCoordinator()
    lifecycle = _configured_owner(tmp_path / "duplicate.sqlite", coordinator)
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    first = lifecycle.command(
        request_id="duplicate-suspend", instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
    )
    replay = lifecycle.command(
        request_id="duplicate-suspend", instance_id=instance.instance_id,
        command=StrategyLifecycleCommand.SUSPEND, expected_generation=0, actor="operator",
    )
    assert first.instance.state is StrategyLifecycleState.SUSPENDED
    assert replay.replayed is True
    assert replay.core_receipt == first.core_receipt
    assert len(coordinator.requests) == 1


def test_real_core_sqlite_suspend_recovers_lost_lifecycle_ack(tmp_path, monkeypatch):
    lifecycle_path = tmp_path / "runner-lifecycle.sqlite"
    core_path = tmp_path / "core-lifecycle.sqlite"
    refs = (CoreAuthorityReference("paper-admission", "admission-1", _DIGEST),)
    writer = CoreWriterIdentity("core-owner", 3)
    now = datetime(2026, 10, 25, tzinfo=UTC)

    def open_core(instance):
        binding = CoreLifecycleExpectedGeneration(
            instance.instance_id,
            instance.incarnation_id,
            instance.package_id,
            instance.package_digest,
            0,
        )
        broker = BrokerSimulator(
            EngineConfig(close_open_position_at_end=False),
            Journal(run_id="synthetic-bootstrap"),
            InstrumentSpec("BTCUSDT"),
        )
        port = SQLiteCoreLifecycleCommitPort(
            core_path,
            binding=binding,
            writer=writer,
            initial_broker=broker,
            trusted_authority_refs=refs,
            initial_risk_enabled=True,
            clock=lambda: now,
        )
        return port, binding

    first_store = SQLiteStrategyLifecycleStore(lifecycle_path)
    first = StrategyLifecycleOwner(
        first_store,
        audit_approved_for_paper=lambda _package_id, _digest: True,
        clock=lambda: now,
    )
    instance = first.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    first_core, _ = open_core(instance)

    def build_transition(*, request_id, instance, **_lifecycle_generations):
        core_binding = first_core.snapshot().lifecycle
        payload = b'{"action":"SUSPEND"}'
        return CoreFenceTransition(
            request_id,
            core_binding,
            writer,
            core_binding.expected_generation + 1,
            CoreLifecycleAction.SUSPEND,
            hashlib.sha256(payload).hexdigest(),
            payload,
            refs,
        )

    first_coordinator = CoreLifecycleCommitCoordinator(first_core)
    first_results = []
    first_commit = first_coordinator.commit_transition

    def record_first_core_commit(request):
        result = first_commit(request)
        first_results.append(result)
        return result

    monkeypatch.setattr(first_coordinator, "commit_transition", record_first_core_commit)
    first._core_request_builder = build_transition
    first._core_coordinator = first_coordinator

    def lose_lifecycle_ack(*_args, **_kwargs):
        raise OSError("injected loss after Core commit")

    monkeypatch.setattr(first, "_apply_core_confirmation", lose_lifecycle_ack)
    with pytest.raises(OSError, match="after Core commit"):
        first.command(
            request_id="real-core-suspend",
            instance_id=instance.instance_id,
            command=StrategyLifecycleCommand.SUSPEND,
            expected_generation=0,
            actor="synthetic-test",
        )

    saved_transition = first_store._db.execute(
        "SELECT core_request_json FROM strategy_lifecycle_core_outbox WHERE request_id=?",
        ("real-core-suspend",),
    ).fetchone()[0]
    committed_snapshot = first_core.snapshot()
    assert len(first_results) == 1
    assert first_results[0].status is CoreCommitStatus.COMMITTED
    assert committed_snapshot.lifecycle.expected_generation == 1
    assert committed_snapshot.commit_sequence == 1
    assert len(committed_snapshot.events) == 1
    assert committed_snapshot.events[0].event_type == "core.lifecycle.suspended"
    assert committed_snapshot.events[0].sequence == 1
    first_store.close()
    first_core.close()

    recovered_store = SQLiteStrategyLifecycleStore(lifecycle_path)
    recovered = StrategyLifecycleOwner(
        recovered_store,
        audit_approved_for_paper=lambda _package_id, _digest: True,
        clock=lambda: now,
    )
    recovered_core, _ = open_core(instance)
    recovered_coordinator = CoreLifecycleCommitCoordinator(recovered_core)
    recovered_results = []
    recovered_commit = recovered_coordinator.commit_transition

    def record_replayed_core_commit(request):
        result = recovered_commit(request)
        recovered_results.append(result)
        return result

    monkeypatch.setattr(
        recovered_coordinator, "commit_transition", record_replayed_core_commit
    )
    recovered._core_request_builder = lambda **_kwargs: pytest.fail(
        "recovery must use the immutable saved outbox transition"
    )
    recovered._core_coordinator = recovered_coordinator
    result = recovered.reconcile_suspend("real-core-suspend")

    assert len(recovered_results) == 1
    assert recovered_results[0].status is CoreCommitStatus.COMMITTED
    assert recovered_results[0].replayed is True
    assert recovered_results[0].receipt == first_results[0].receipt
    assert result.instance.state is StrategyLifecycleState.SUSPENDED
    assert result.enforcement_status == "ENFORCED"
    assert result.core_receipt == first_results[0].receipt
    assert recovered_store._db.execute(
        "SELECT core_request_json,status,receipt_sha256 FROM strategy_lifecycle_core_outbox "
        "WHERE request_id=?",
        ("real-core-suspend",),
    ).fetchone() == (
        saved_transition,
        "ENFORCED",
        hashlib.sha256(
            recovered_store._db.execute(
                "SELECT receipt_json FROM strategy_lifecycle_core_outbox WHERE request_id=?",
                ("real-core-suspend",),
            ).fetchone()[0].encode("utf-8")
        ).hexdigest(),
    )
    final_snapshot = recovered_core.snapshot()
    assert final_snapshot.commit_sequence == 1
    assert len(final_snapshot.events) == 1
    assert final_snapshot.events[0].event_type == "core.lifecycle.suspended"
    assert final_snapshot.events[0].sequence == 1
    with sqlite3.connect(core_path) as core_db:
        assert core_db.execute(
            "SELECT request_id,request_kind,commit_sequence FROM core_lifecycle_requests"
        ).fetchall() == [("real-core-suspend", "transition", 1)]
    recovered_store.close()
    recovered_core.close()


def test_concurrent_same_request_is_idempotent_and_different_command_conflicts(tmp_path):
    lifecycle = owner(tmp_path / "lifecycle.sqlite")
    instance = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    service = StrategyLifecycleService(lifecycle)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: service.suspend("same-request", instance.instance_id, 0, "operator"),
                range(8),
            )
        )
    assert sum(not result.replayed for result in results) == 1
    assert all(
        result.instance.state is StrategyLifecycleState.SUSPEND_PENDING for result in results
    )
    assert all(result.expected_lifecycle_generation == 0 for result in results)
    assert all(result.resulting_lifecycle_generation == 1 for result in results)
    assert all(result.enforcement_status == "PENDING_CORE_COMMIT" for result in results)
    with pytest.raises(LifecycleCommandError, match="idempotency conflict"):
        lifecycle.command(
            request_id="same-request",
            instance_id=instance.instance_id,
            command=StrategyLifecycleCommand.RESUME,
            expected_generation=0,
            actor="operator",
        )
    with pytest.raises(LifecycleCommandError, match="pending Core fence"):
        service.resume("resume-pending", instance.instance_id, 1, "operator")
    assert lifecycle.get_instance(instance.instance_id).lifecycle_generation == 1


def test_expected_generation_cas_rejects_stale_and_concurrent_commands(tmp_path):
    db_path = tmp_path / "generation-race.sqlite"
    first = owner(db_path)
    instance = first.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    second = owner(db_path)
    barrier = threading.Barrier(2)

    def suspend(owner_instance: StrategyLifecycleOwner, request_id: str):
        barrier.wait(timeout=5)
        try:
            return owner_instance.command(
                request_id=request_id,
                instance_id=instance.instance_id,
                command=StrategyLifecycleCommand.SUSPEND,
                expected_generation=0,
                actor="operator",
            )
        except LifecycleGenerationConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: suspend(*args), ((first, "s1"), (second, "s2"))))
    assert sum(not isinstance(value, LifecycleGenerationConflict) for value in outcomes) == 1
    assert sum(isinstance(value, LifecycleGenerationConflict) for value in outcomes) == 1
    current = first.get_instance(instance.instance_id)
    assert current.lifecycle_generation == 1
    assert current.state is StrategyLifecycleState.SUSPEND_PENDING
    with pytest.raises(LifecycleGenerationConflict, match="current 1"):
        first.command(
            request_id="stale",
            instance_id=instance.instance_id,
            command=StrategyLifecycleCommand.SUSPEND,
            expected_generation=0,
            actor="operator",
        )
    assert first.get_instance(instance.instance_id).lifecycle_generation == 1
    first.store.close()
    second.store.close()


def test_create_and_clone_remain_inert_and_supersession_fails_closed(tmp_path):
    lifecycle = owner(tmp_path / "lifecycle.sqlite")
    source = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-shared"
    )
    clone = lifecycle.clone_instance(source.instance_id, runner_id="runner-shared")
    assert clone.instance_id != source.instance_id
    assert clone.incarnation_id != source.incarnation_id
    assert clone.package_id == source.package_id
    assert clone.package_digest == source.package_digest
    for registered in (source, clone):
        assert registered.state is StrategyLifecycleState.PENDING_AUTHORIZATION
        assert registered.governance_running is False
        assert registered.admission_id is None
    with pytest.raises(sqlite3.IntegrityError, match="authorization source unavailable"):
        lifecycle.store._db.execute(
            "UPDATE strategy_instances SET governance_running=1 WHERE instance_id=?",
            (source.instance_id,),
        )
    before = lifecycle.store._db.execute("SELECT COUNT(*) FROM strategy_instances").fetchone()[0]
    with pytest.raises(LifecycleCommandError, match="atomic owner boundary"):
        lifecycle.supersede(
            source.instance_id,
            package_id="pkg-b",
            package_digest="b" * 64,
            runner_id="runner-shared",
        )
    assert (
        lifecycle.store._db.execute("SELECT COUNT(*) FROM strategy_instances").fetchone()[0]
        == before
    )
    assert (
        lifecycle.get_instance(source.instance_id).state
        is StrategyLifecycleState.PENDING_AUTHORIZATION
    )


def test_independent_owners_cannot_supersede_without_atomic_authority(tmp_path):
    db_path = tmp_path / "shared-lifecycle.sqlite"
    first_owner = owner(db_path)
    source = first_owner.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-shared"
    )
    second_owner = owner(db_path)

    def replace(owner_instance: StrategyLifecycleOwner, package: str):
        try:
            return owner_instance.supersede(
                source.instance_id,
                package_id=package,
                package_digest=(package == "pkg-b" and "b" or "c") * 64,
                runner_id="runner-shared",
            )
        except LifecycleCommandError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda args: replace(*args),
                ((first_owner, "pkg-b"), (second_owner, "pkg-c")),
            )
        )
    assert all(isinstance(value, LifecycleCommandError) for value in outcomes)
    assert (
        first_owner.get_instance(source.instance_id).state
        is StrategyLifecycleState.PENDING_AUTHORIZATION
    )
    assert (
        first_owner.store._db.execute("SELECT COUNT(*) FROM strategy_instances").fetchone()[0] == 1
    )
    first_owner.store.close()
    second_owner.store.close()


def test_instances_share_package_and_runner_but_keep_owner_identity(tmp_path):
    lifecycle = owner(tmp_path / "lifecycle.sqlite")
    first = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-shared"
    )
    second = lifecycle.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-shared"
    )
    assert first.instance_id != second.instance_id
    assert first.incarnation_id != second.incarnation_id
    assert first.runner_id == second.runner_id
    assert first.package_digest == second.package_digest
    observed = lifecycle.observe_runner_state(first.instance_id, "DEGRADED")
    assert observed.observed_runner_state == "DEGRADED"
    assert observed.governance_running is False
    assert observed.state is StrategyLifecycleState.PENDING_AUTHORIZATION


def test_resume_fails_closed_and_audit_guard_is_independent(tmp_path):
    lifecycle = owner(tmp_path / "lifecycle.sqlite", approved=False)
    with pytest.raises(LifecycleCommandError, match="APPROVED_FOR_PAPER"):
        lifecycle.create_instance(package_id="pkg-no", package_digest=_DIGEST, runner_id="runner-1")
    truthy_only = owner(tmp_path / "truthy.sqlite", approved="APPROVED_FOR_PAPER")
    with pytest.raises(LifecycleCommandError, match="APPROVED_FOR_PAPER"):
        truthy_only.create_instance(
            package_id="pkg-truthy", package_digest=_DIGEST, runner_id="runner-1"
        )
    assert (
        truthy_only.store._db.execute("SELECT COUNT(*) FROM strategy_instances").fetchone()[0] == 0
    )

    approved = owner(tmp_path / "approved.sqlite")
    instance = approved.create_instance(
        package_id="pkg-a", package_digest=_DIGEST, runner_id="runner-1"
    )
    service = StrategyLifecycleService(approved)
    with pytest.raises(LifecycleCommandError, match="commit-time currentness"):
        service.resume("resume", instance.instance_id, 0, "operator")
    assert (
        approved.get_instance(instance.instance_id).state
        is StrategyLifecycleState.PENDING_AUTHORIZATION
    )


def test_future_schema_database_is_rejected_without_mutation(tmp_path):
    db_path = tmp_path / "future.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE strategy_lifecycle_schema(version INTEGER PRIMARY KEY);"
        "INSERT INTO strategy_lifecycle_schema(version) VALUES(6);"
        "CREATE TABLE sentinel(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO sentinel VALUES('marker','keep-me');"
    )
    before_schema = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    before_data = (
        connection.execute("SELECT version FROM strategy_lifecycle_schema").fetchall(),
        connection.execute("SELECT key,value FROM sentinel").fetchall(),
    )
    before_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()

    with pytest.raises(LifecycleCommandError, match="schema unsupported"):
        SQLiteStrategyLifecycleStore(db_path)

    verify = sqlite3.connect(db_path)
    assert (
        verify.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        == before_schema
    )
    after_data = (
        verify.execute("SELECT version FROM strategy_lifecycle_schema").fetchall(),
        verify.execute("SELECT key,value FROM sentinel").fetchall(),
    )
    assert after_data == before_data
    assert verify.execute("PRAGMA journal_mode").fetchone()[0] == before_journal_mode
    verify.close()


def test_v1_database_migrates_to_generation_schema_and_keeps_identity(tmp_path):
    db_path = tmp_path / "legacy-v1.sqlite"
    create_v1_database(db_path, state="SUSPENDED")

    legacy_writer = sqlite3.connect(db_path)
    store = SQLiteStrategyLifecycleStore(db_path)
    version = store._db.execute("SELECT version FROM strategy_lifecycle_schema").fetchone()[0]
    assert version == 5
    with pytest.raises(sqlite3.IntegrityError, match="generation transition"):
        legacy_writer.execute(
            "UPDATE strategy_instances SET state='SUSPENDED' WHERE instance_id='si_old'"
        )
    legacy_writer.rollback()
    owner_instance = StrategyLifecycleOwner(
        store,
        audit_approved_for_paper=lambda _package, _digest: True,
        clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
    )
    migrated = owner_instance.get_instance("si_old")
    assert migrated.incarnation_id == "inc_old"
    assert migrated.state is StrategyLifecycleState.SUSPEND_PENDING
    assert migrated.lifecycle_generation == 1
    assert migrated.core_fence_status == "PENDING_CORE_COMMIT"
    guard_sql = store._db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='strategy_instance_generation_guard'"
    ).fetchone()[0]
    assert "receipt_sha256 IS NOT NULL" in guard_sql
    replay = owner_instance.command(
        request_id="legacy-suspend",
        instance_id="si_old",
        command=StrategyLifecycleCommand.SUSPEND,
        expected_generation=0,
        actor="operator",
    )
    assert replay.replayed is True
    assert replay.instance.state is StrategyLifecycleState.SUSPEND_PENDING
    assert replay.enforcement_status == "PENDING_CORE_COMMIT"
    assert owner_instance.get_instance("si_old").lifecycle_generation == 1
    legacy_writer.close()
    store.close()


def test_concurrent_v1_migration_openers_serialize_and_both_accept_v5(tmp_path, monkeypatch):
    db_path = tmp_path / "concurrent-legacy-v1.sqlite"
    create_v1_database(db_path, state="PENDING_AUTHORIZATION")
    barrier = threading.Barrier(2)
    preflight_threads = set()
    original_schema_version = SQLiteStrategyLifecycleStore._schema_version

    def synchronized_preflight(store):
        version = original_schema_version(store)
        thread_id = threading.get_ident()
        if version == 1 and thread_id not in preflight_threads:
            preflight_threads.add(thread_id)
            barrier.wait(timeout=5)
        return version

    monkeypatch.setattr(SQLiteStrategyLifecycleStore, "_schema_version", synchronized_preflight)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: SQLiteStrategyLifecycleStore(db_path), range(2)))
    try:
        assert all(
            store._db.execute("SELECT version FROM strategy_lifecycle_schema").fetchone()[0] == 5
            for store in stores
        )
        assert all(
            store._db.execute(
                "SELECT state FROM strategy_instances WHERE instance_id='si_old'"
            ).fetchone()[0]
            == StrategyLifecycleState.PENDING_AUTHORIZATION.value
            for store in stores
        )
    finally:
        for store in stores:
            store.close()


def test_core_validation_and_commit_receipts_are_distinct_immutable_contracts():
    payload = b'{"instrument":"TEST","quantity":1}'
    payload_digest = hashlib.sha256(payload).hexdigest()
    lifecycle = CoreLifecycleExpectedGeneration("si-1", "inc-1", "pkg-1", _DIGEST, 7)
    writer = CoreWriterIdentity("core-owner-1", 4)
    authority = CoreAuthorityReference("admission", "admission-1", _DIGEST)
    effect = CoreLifecycleEffect(
        "effect-1",
        lifecycle,
        writer,
        "submit_order",
        payload_digest,
        payload,
        ("order-1",),
        (authority,),
    )
    effect_validation = CoreEffectValidation("effect-1", _DIGEST, writer, 7, True, "preflight only")
    transition = CoreFenceTransition(
        "transition-1",
        lifecycle,
        writer,
        8,
        CoreLifecycleAction.SUSPEND,
        payload_digest,
        payload,
        (authority,),
    )
    fence_validation = CoreFenceValidation(
        "transition-1", _DIGEST, writer, 7, True, "preflight only"
    )
    committed = CoreFenceTransitionReceipt(
        "transition-1",
        "si-1",
        "inc-1",
        "pkg-1",
        _DIGEST,
        writer,
        7,
        8,
        CoreLifecycleAction.SUSPEND,
        payload_digest,
        (authority,),
        10,
        datetime(2026, 10, 25, tzinfo=UTC),
    )
    assert effect.lifecycle == lifecycle
    assert effect_validation.allowed
    assert committed.matches_request(transition)
    assert committed.same_commit(committed)
    assert transition.resulting_generation == committed.resulting_generation
    assert fence_validation.observed_generation == 7
    effect_receipt = CoreEffectCommitReceipt(
        "effect-1",
        "si-1",
        "inc-1",
        "pkg-1",
        _DIGEST,
        writer,
        7,
        payload_digest,
        "submit_order",
        ("order-1",),
        (authority,),
        11,
        datetime(2026, 10, 25, tzinfo=UTC),
    )
    assert effect_receipt.matches_request(effect)
    assert effect_receipt.same_commit(effect_receipt)
    with pytest.raises(FrozenInstanceError):
        lifecycle.expected_generation = 8  # type: ignore[misc]
    with pytest.raises(ValueError, match="advance exactly once"):
        CoreFenceTransition(
            "bad",
            lifecycle,
            writer,
            9,
            CoreLifecycleAction.SUSPEND,
            payload_digest,
            payload,
            (authority,),
        )


def test_core_commit_coordinator_fails_closed_without_canonical_provider():
    coordinator = CoreLifecycleCommitCoordinator()
    result = coordinator.commit_effect(_canonical_effect_request())
    assert result.status is CoreCommitStatus.UNAVAILABLE
    assert result.receipt is None
    transition_result = coordinator.commit_transition(_canonical_transition_request())
    assert transition_result.status is CoreCommitStatus.UNAVAILABLE
    assert transition_result.receipt is None


def test_core_commit_coordinator_publishes_receipt_only_after_atomic_commit():
    port = _SyntheticCanonicalPort()
    result = CoreLifecycleCommitCoordinator(port).commit_effect(_canonical_effect_request())
    assert result.status is CoreCommitStatus.COMMITTED
    assert result.receipt is not None
    assert port.transaction.commit_calls == 1
    assert port.transaction.staged_effects == 1
    assert port.transaction.rollbacks == 0


def test_core_commit_coordinator_preserves_canonical_idempotent_replay():
    port = _SyntheticCanonicalPort(replayed=True)
    result = CoreLifecycleCommitCoordinator(port).commit_effect(_canonical_effect_request())
    assert result.status is CoreCommitStatus.COMMITTED
    assert result.replayed is True


def test_core_commit_coordinator_returns_unresolved_without_retrying_ambiguous_commit():
    port = _SyntheticCanonicalPort(ambiguous=True)
    coordinator = CoreLifecycleCommitCoordinator(port)
    result = coordinator.commit_effect(_canonical_effect_request())
    assert result.status is CoreCommitStatus.UNRESOLVED
    assert result.receipt is None
    assert port.begin_calls == 1
    assert port.transaction.staged_effects == 1
    assert port.transaction.commit_calls == 1
    assert port.transaction.rollbacks == 0
    assert port.transaction.released is True


def test_core_commit_coordinator_rolls_back_ambiguous_stage_failure():
    port = _SyntheticCanonicalPort(stage_ambiguous=True)
    result = CoreLifecycleCommitCoordinator(port).commit_effect(_canonical_effect_request())
    assert result.status is CoreCommitStatus.UNAVAILABLE
    assert result.receipt is None
    assert port.transaction.rollbacks == 1
    assert port.transaction.commit_calls == 0


def test_core_commit_coordinator_rolls_back_rejection_and_rejects_mismatched_receipt():
    rejected = _SyntheticCanonicalPort(reject=True)
    denied = CoreLifecycleCommitCoordinator(rejected).commit_effect(_canonical_effect_request())
    assert denied.status is CoreCommitStatus.DENIED
    assert rejected.transaction.rollbacks == 1
    assert rejected.transaction.commit_calls == 0

    mismatched = _SyntheticCanonicalPort(mismatch=True)
    denied_receipt = CoreLifecycleCommitCoordinator(mismatched).commit_effect(
        _canonical_effect_request()
    )
    assert denied_receipt.status is CoreCommitStatus.DENIED
    assert denied_receipt.receipt is None
    assert mismatched.transaction.rollbacks == 1
    assert mismatched.transaction.commit_calls == 0

    invalid_wrapper = _SyntheticCanonicalPort(invalid_wrapper=True)
    invalid = CoreLifecycleCommitCoordinator(invalid_wrapper).commit_effect(
        _canonical_effect_request()
    )
    assert invalid.status is CoreCommitStatus.UNAVAILABLE
    assert invalid.receipt is None
    assert invalid_wrapper.transaction.rollbacks == 1
    assert invalid_wrapper.transaction.commit_calls == 0

    invalid_receipt = _SyntheticCanonicalPort(invalid_receipt=True)
    invalid = CoreLifecycleCommitCoordinator(invalid_receipt).commit_effect(
        _canonical_effect_request()
    )
    assert invalid.status is CoreCommitStatus.UNAVAILABLE
    assert invalid.receipt is None
    assert invalid_receipt.transaction.rollbacks == 1
    assert invalid_receipt.transaction.commit_calls == 0

    invalid_replay = _SyntheticCanonicalPort(invalid_replayed=True)
    invalid = CoreLifecycleCommitCoordinator(invalid_replay).commit_effect(
        _canonical_effect_request()
    )
    assert invalid.status is CoreCommitStatus.UNAVAILABLE
    assert invalid.receipt is None
    assert invalid_replay.transaction.rollbacks == 1
    assert invalid_replay.transaction.commit_calls == 0


def test_core_commit_result_revalidates_concrete_receipt():
    request = _canonical_effect_request()
    staged = _SyntheticCanonicalTransaction(_SyntheticCanonicalPort()).check_and_stage_effect(
        request
    )
    object.__setattr__(staged.receipt, "canonical_commit_sequence", 0)
    with pytest.raises(ValueError, match="positive integer"):
        CoreLifecycleCommitResult(CoreCommitStatus.COMMITTED, staged.receipt, False, "committed")


def test_core_commit_coordinator_routes_fence_transition_through_same_transaction():
    port = _SyntheticCanonicalPort()
    request = _canonical_transition_request()
    result = CoreLifecycleCommitCoordinator(port).commit_transition(request)
    assert result.status is CoreCommitStatus.COMMITTED
    assert isinstance(result.receipt, CoreFenceTransitionReceipt)
    assert result.receipt.matches_request(request)
    assert port.transaction.commit_calls == 1
    assert port.transaction.staged_transitions == 1


def _canonical_effect_request():
    payload = b'{"operation":"submit_order","instrument":"TEST","quantity":1}'
    claim = CoreLifecycleExpectedGeneration("instance", "incarnation", "package", _DIGEST, 3)
    return CoreLifecycleEffect(
        "effect-id",
        claim,
        CoreWriterIdentity("canonical-core", 2),
        "submit_order",
        hashlib.sha256(payload).hexdigest(),
        payload,
        ("order-target",),
        (),
    )


def _canonical_transition_request():
    payload = b'{"action":"SUSPEND"}'
    claim = CoreLifecycleExpectedGeneration("instance", "incarnation", "package", _DIGEST, 3)
    return CoreFenceTransition(
        "transition-id",
        claim,
        CoreWriterIdentity("canonical-core", 2),
        4,
        CoreLifecycleAction.SUSPEND,
        hashlib.sha256(payload).hexdigest(),
        payload,
        (),
    )


class _SyntheticCanonicalPort:
    def __init__(
        self,
        *,
        ambiguous=False,
        stage_ambiguous=False,
        reject=False,
        mismatch=False,
        replayed=False,
        invalid_wrapper=False,
        invalid_receipt=False,
        invalid_replayed=False,
    ):
        self.ambiguous = ambiguous
        self.stage_ambiguous = stage_ambiguous
        self.reject = reject
        self.mismatch = mismatch
        self.replayed = replayed
        self.invalid_wrapper = invalid_wrapper
        self.invalid_receipt = invalid_receipt
        self.invalid_replayed = invalid_replayed
        self.begin_calls = 0
        self.transaction = _SyntheticCanonicalTransaction(self)

    def begin_lifecycle_transaction(self):
        self.begin_calls += 1
        return self.transaction


class _SyntheticCanonicalTransaction:
    def __init__(self, port):
        self.port = port
        self.staged_effects = 0
        self.staged_transitions = 0
        self.commit_calls = 0
        self.rollbacks = 0
        self.released = False

    def check_and_stage_effect(self, request):
        if self.port.reject:
            raise CoreAtomicCommitRejected("synthetic stale generation")
        if self.port.stage_ambiguous:
            raise CoreAtomicCommitOutcomeUnknown("synthetic stage adapter uncertainty")
        if self.port.invalid_wrapper:
            return object()
        self.staged_effects += 1
        claim = request.lifecycle
        receipt = CoreEffectCommitReceipt(
            "other-effect" if self.port.mismatch else request.effect_id,
            claim.instance_id,
            claim.incarnation_id,
            claim.package_id,
            claim.package_digest,
            request.writer,
            claim.expected_generation,
            request.canonical_payload_digest,
            request.operation,
            request.target_ids,
            request.authority_refs,
            51,
            datetime(2026, 10, 25, tzinfo=UTC),
        )
        if self.port.invalid_receipt:
            object.__setattr__(receipt, "canonical_commit_sequence", 0)
        staged = CoreStagedEffectCommit(receipt, replayed=self.port.replayed)
        if self.port.invalid_replayed:
            object.__setattr__(staged, "replayed", 1)
        return staged

    def check_and_stage_transition(self, request):
        self.staged_transitions += 1
        claim = request.lifecycle
        receipt = CoreFenceTransitionReceipt(
            request.transition_id,
            claim.instance_id,
            claim.incarnation_id,
            claim.package_id,
            claim.package_digest,
            request.writer,
            claim.expected_generation,
            request.resulting_generation,
            request.action,
            request.canonical_payload_digest,
            request.authority_refs,
            52,
            datetime(2026, 10, 25, tzinfo=UTC),
        )
        return CoreStagedTransitionCommit(receipt, replayed=False)

    def commit(self):
        self.commit_calls += 1
        try:
            if self.port.ambiguous:
                raise CoreAtomicCommitOutcomeUnknown("synthetic lost acknowledgement")
        finally:
            self.released = True

    def rollback(self):
        self.rollbacks += 1
        self.released = True
