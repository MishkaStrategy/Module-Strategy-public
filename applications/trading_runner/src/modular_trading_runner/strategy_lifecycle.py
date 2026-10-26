from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from modular_core.lifecycle import (
    CoreAuthorityReference,
    CoreCommitStatus,
    CoreFenceTransition,
    CoreFenceTransitionReceipt,
    CoreLifecycleAction,
    CoreLifecycleCommitCoordinator,
    CoreLifecycleCommitResult,
    CoreLifecycleExpectedGeneration,
    CoreWriterIdentity,
)


class LifecycleCommandError(RuntimeError):
    """A strategy lifecycle command failed closed."""


class StrategyLifecycleState(StrEnum):
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    SUSPEND_PENDING = "SUSPEND_PENDING"
    SUSPENDED = "SUSPENDED"
    SUPERSEDED = "SUPERSEDED"


class StrategyLifecycleCommand(StrEnum):
    SUSPEND = "SUSPEND"
    RESUME = "RESUME"


@dataclass(frozen=True, slots=True)
class StrategyInstance:
    """Lifecycle snapshot; its revision does not prove Core enforced that revision."""

    instance_id: str
    incarnation_id: str
    package_id: str
    package_digest: str
    runner_id: str
    state: StrategyLifecycleState
    governance_running: bool
    observed_runner_state: str
    supersedes_instance_id: str | None
    cancellation_status: str
    reconciliation_status: str
    admission_id: str | None
    lifecycle_generation: int
    core_fence_status: str


@dataclass(frozen=True, slots=True)
class LifecycleCommandResult:
    """The resulting generation is Lifecycle intent state, not Core enforcement."""

    request_id: str
    command: StrategyLifecycleCommand
    instance: StrategyInstance
    replayed: bool
    expected_lifecycle_generation: int
    resulting_lifecycle_generation: int
    enforcement_status: str
    core_receipt: CoreFenceTransitionReceipt | None = None


class LifecycleGenerationConflict(LifecycleCommandError):
    def __init__(self, expected: int, current: int) -> None:
        super().__init__(f"stale lifecycle generation: expected {expected}, current {current}")
        self.expected_lifecycle_generation = expected
        self.current_lifecycle_generation = current


class SQLiteStrategyLifecycleStore:
    """Transactional SQLite store owned by Strategy Lifecycle."""

    def __init__(self, path: str | Path) -> None:
        target = Path(path).expanduser()
        if target.is_symlink():
            raise LifecycleCommandError("lifecycle database cannot be a symlink")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self._lock = threading.RLock()
        self._db = sqlite3.connect(target, timeout=30, check_same_thread=False)
        try:
            self._initialize_schema()
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA journal_mode=WAL")
        except BaseException:
            self._db.close()
            raise

    def _initialize_schema(self) -> None:
        with self._lock:
            if self._user_objects():
                self._upgrade_or_validate_schema()
                return
            if self._db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                raise LifecycleCommandError("lifecycle database contains an unknown schema")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have initialized after our first read.
                if self._user_objects():
                    self._db.execute("ROLLBACK")
                    self._upgrade_or_validate_schema()
                    return
                if self._db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                    raise LifecycleCommandError("lifecycle database contains an unknown schema")
                self._db.execute(
                    "CREATE TABLE strategy_lifecycle_schema(version INTEGER PRIMARY KEY)"
                )
                self._db.execute("INSERT INTO strategy_lifecycle_schema(version) VALUES(5)")
                self._db.execute(
                    "CREATE TABLE strategy_instances("
                    "instance_id TEXT PRIMARY KEY, incarnation_id TEXT NOT NULL UNIQUE,"
                    "package_id TEXT NOT NULL, package_digest TEXT NOT NULL,"
                    "runner_id TEXT NOT NULL, state TEXT NOT NULL,"
                    "governance_running INTEGER NOT NULL, observed_runner_state TEXT NOT NULL,"
                    "supersedes_instance_id TEXT REFERENCES strategy_instances(instance_id),"
                    "cancellation_status TEXT NOT NULL, reconciliation_status TEXT NOT NULL,"
                    "admission_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                    "lifecycle_generation INTEGER NOT NULL DEFAULT 0 CHECK(lifecycle_generation>=0),"
                    "core_fence_status TEXT NOT NULL DEFAULT 'UNAVAILABLE',"
                    "CHECK(state<>'RUNNING'), CHECK(governance_running=0), CHECK(admission_id IS NULL))"
                )
                self._db.execute(
                    "CREATE INDEX strategy_instances_package "
                    "ON strategy_instances(package_id, package_digest)"
                )
                self._db.execute(
                    "CREATE INDEX strategy_instances_runner ON strategy_instances(runner_id)"
                )
                self._db.execute(
                    "CREATE TRIGGER strategy_instance_binding_immutable "
                    "BEFORE UPDATE OF instance_id,incarnation_id,package_id,package_digest,runner_id "
                    "ON strategy_instances BEGIN SELECT RAISE(ABORT, "
                    "'strategy instance identity and package binding are immutable'); END"
                )
                self._db.execute(
                    "CREATE TRIGGER strategy_instance_authorization_insert_guard "
                    "BEFORE INSERT ON strategy_instances WHEN NEW.state='RUNNING' "
                    "OR NEW.governance_running<>0 OR NEW.admission_id IS NOT NULL "
                    "BEGIN SELECT RAISE(ABORT, 'strategy authorization source unavailable'); END"
                )
                self._db.execute(
                    "CREATE TRIGGER strategy_instance_authorization_update_guard "
                    "BEFORE UPDATE OF state,governance_running,admission_id ON strategy_instances "
                    "WHEN NEW.state='RUNNING' OR NEW.governance_running<>0 "
                    "OR NEW.admission_id IS NOT NULL "
                    "BEGIN SELECT RAISE(ABORT, 'strategy authorization source unavailable'); END"
                )
                self._create_core_outbox_schema()
                self._create_generation_guard()
                self._db.execute(
                    "CREATE TABLE strategy_lifecycle_commands("
                    "request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,"
                    "result_json TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                self._db.execute(
                    "CREATE TABLE strategy_lifecycle_audit("
                    "event_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
                    "request_id TEXT NOT NULL, command TEXT NOT NULL, instance_id TEXT NOT NULL,"
                    "actor TEXT NOT NULL, result TEXT NOT NULL, previous_state TEXT NOT NULL,"
                    "new_state TEXT NOT NULL)"
                )
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _user_tables(self) -> set[str]:
        return {
            str(row[0])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _user_objects(self) -> set[tuple[str, str]]:
        return {
            (str(row[0]), str(row[1]))
            for row in self._db.execute(
                "SELECT type,name FROM sqlite_master "
                "WHERE type IN ('table','view','index','trigger') "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _schema_version(self) -> int:
        if "strategy_lifecycle_schema" in self._user_tables():
            try:
                versions = tuple(
                    row[0]
                    for row in self._db.execute(
                        "SELECT version FROM strategy_lifecycle_schema ORDER BY version"
                    )
                )
            except sqlite3.DatabaseError:
                raise LifecycleCommandError("lifecycle database schema unsupported") from None
            if len(versions) == 1 and isinstance(versions[0], int):
                return versions[0]
            raise LifecycleCommandError("lifecycle database schema unsupported")
        else:
            raise LifecycleCommandError("lifecycle database is unrelated or incomplete")

    def _upgrade_or_validate_schema(self) -> None:
        # Reject unsupported versions from a read-only preflight before acquiring
        # a writer lock. The version and exact shape are then re-read under the
        # lock so a concurrent migration is accepted in its committed state.
        if self._schema_version() not in {1, 2, 3, 4, 5}:
            raise LifecycleCommandError("lifecycle database schema unsupported")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            version = self._schema_version()
            if version not in {1, 2, 3, 4, 5}:
                raise LifecycleCommandError("lifecycle database schema unsupported")
            self._validate_schema_shape(version)
            if version == 1:
                legacy_commands = self._db.execute(
                    "SELECT request_id,result_json FROM strategy_lifecycle_commands"
                ).fetchall()
                self._db.execute(
                    "ALTER TABLE strategy_instances ADD COLUMN lifecycle_generation INTEGER "
                    "NOT NULL DEFAULT 0 CHECK(lifecycle_generation>=0)"
                )
                self._db.execute(
                    "ALTER TABLE strategy_instances ADD COLUMN core_fence_status TEXT "
                    "NOT NULL DEFAULT 'UNAVAILABLE'"
                )
                self._db.execute(
                    "UPDATE strategy_instances SET state='SUSPEND_PENDING', "
                    "core_fence_status='PENDING_CORE_COMMIT', lifecycle_generation=1 "
                    "WHERE state='SUSPENDED'"
                )
                for request_id, raw_result in legacy_commands:
                    legacy_instance = json.loads(raw_result)
                    if not isinstance(legacy_instance, dict):
                        raise LifecycleCommandError("legacy lifecycle command result is invalid")
                    state = legacy_instance.get("state")
                    if state == "SUSPENDED":
                        state = StrategyLifecycleState.SUSPEND_PENDING.value
                        lifecycle_generation = 1
                        core_fence_status = "PENDING_CORE_COMMIT"
                    else:
                        lifecycle_generation = 0
                        core_fence_status = "UNAVAILABLE"
                    legacy_instance.update(
                        {
                            "state": state,
                            "lifecycle_generation": lifecycle_generation,
                            "core_fence_status": core_fence_status,
                        }
                    )
                    migrated_result = json.dumps(
                        {
                            "instance": legacy_instance,
                            "expected_lifecycle_generation": 0,
                            "resulting_lifecycle_generation": lifecycle_generation,
                            "enforcement_status": core_fence_status,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    self._db.execute(
                        "UPDATE strategy_lifecycle_commands SET result_json=? WHERE request_id=?",
                        (migrated_result, request_id),
                    )
                # The guard and outbox are installed below before this migration commits.
                self._db.execute("UPDATE strategy_lifecycle_schema SET version=3 WHERE version=1")
            elif version == 2:
                self._db.execute("UPDATE strategy_lifecycle_schema SET version=3 WHERE version=2")
            if version < 4:
                # Existing pending commands remain fail-closed: migration does not
                # infer or synthesize a Core transition for them.
                self._db.execute("DROP TRIGGER IF EXISTS strategy_instance_generation_guard")
                self._create_core_outbox_schema()
                self._create_generation_guard()
                self._db.execute("UPDATE strategy_lifecycle_schema SET version=4 WHERE version=3")
            if version < 5:
                columns = {
                    str(row[1])
                    for row in self._db.execute(
                        "PRAGMA table_info(strategy_lifecycle_core_outbox)"
                    )
                }
                if "receipt_sha256" not in columns:
                    self._db.execute(
                        "ALTER TABLE strategy_lifecycle_core_outbox ADD COLUMN receipt_sha256 TEXT"
                    )
                    rows = self._db.execute(
                        "SELECT request_id,receipt_json FROM strategy_lifecycle_core_outbox "
                        "WHERE status='ENFORCED'"
                    ).fetchall()
                    for request_id, receipt_json in rows:
                        self._db.execute(
                            "UPDATE strategy_lifecycle_core_outbox SET receipt_sha256=? "
                            "WHERE request_id=?",
                            (_sha256_text(str(receipt_json)), request_id),
                        )
                # v4's guard predates the durable receipt digest requirement.
                self._create_generation_guard()
                self._db.execute("UPDATE strategy_lifecycle_schema SET version=5")
            self._validate_schema_shape(5)
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _create_generation_guard(self) -> None:
        self._db.execute("DROP TRIGGER IF EXISTS strategy_instance_generation_guard")
        self._db.execute(
            "CREATE TRIGGER strategy_instance_generation_guard "
            "BEFORE UPDATE OF state,lifecycle_generation ON strategy_instances "
            "WHEN NEW.state='RUNNING' OR "
            "((NEW.state<>OLD.state OR NEW.lifecycle_generation<>OLD.lifecycle_generation) "
            "AND NOT ((NEW.state='SUSPEND_PENDING' "
            "AND OLD.state='PENDING_AUTHORIZATION' "
            "AND NEW.lifecycle_generation=OLD.lifecycle_generation+1) OR "
            "(NEW.state='SUSPENDED' AND OLD.state='SUSPEND_PENDING' "
            "AND NEW.lifecycle_generation=OLD.lifecycle_generation AND EXISTS ("
            "SELECT 1 FROM strategy_lifecycle_core_outbox o WHERE "
            "o.instance_id=NEW.instance_id AND o.status='ENFORCED' "
            "AND o.resulting_lifecycle_generation=NEW.lifecycle_generation "
            "AND o.receipt_json IS NOT NULL AND o.receipt_sha256 IS NOT NULL)))) "
            "BEGIN SELECT RAISE(ABORT, 'lifecycle state requires a generation transition'); END"
        )

    def _create_core_outbox_schema(self) -> None:
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS strategy_lifecycle_core_outbox("
            "request_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES strategy_instances(instance_id),"
            "incarnation_id TEXT NOT NULL, package_id TEXT NOT NULL, package_digest TEXT NOT NULL,"
            "expected_lifecycle_generation INTEGER NOT NULL CHECK(expected_lifecycle_generation>=0),"
            "resulting_lifecycle_generation INTEGER NOT NULL CHECK(resulting_lifecycle_generation=expected_lifecycle_generation+1),"
            "core_request_json TEXT NOT NULL, core_request_digest TEXT NOT NULL,"
            "status TEXT NOT NULL CHECK(status IN ('PENDING','ENFORCED')),"
            "receipt_json TEXT, receipt_sha256 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
            "CHECK((status='PENDING' AND receipt_json IS NULL AND receipt_sha256 IS NULL) OR "
            "(status='ENFORCED' AND receipt_json IS NOT NULL AND receipt_sha256 IS NOT NULL)))"
        )
        self._db.execute(
            "CREATE TRIGGER IF NOT EXISTS strategy_lifecycle_outbox_request_immutable "
            "BEFORE UPDATE OF request_id,instance_id,incarnation_id,package_id,package_digest,"
            "expected_lifecycle_generation,resulting_lifecycle_generation,core_request_json,"
            "core_request_digest,created_at ON strategy_lifecycle_core_outbox "
            "BEGIN SELECT RAISE(ABORT, 'Core lifecycle outbox request is immutable'); END"
        )

    def _validate_schema_shape(self, version: int) -> None:
        expected_objects = {
            ("table", "strategy_lifecycle_schema"),
            ("table", "strategy_instances"),
            ("table", "strategy_lifecycle_commands"),
            ("table", "strategy_lifecycle_audit"),
            ("index", "strategy_instances_package"),
            ("index", "strategy_instances_runner"),
            ("trigger", "strategy_instance_binding_immutable"),
            ("trigger", "strategy_instance_authorization_insert_guard"),
            ("trigger", "strategy_instance_authorization_update_guard"),
        }
        if version >= 3:
            expected_objects.add(("trigger", "strategy_instance_generation_guard"))
        if version >= 4:
            expected_objects.add(("table", "strategy_lifecycle_core_outbox"))
            expected_objects.add(("trigger", "strategy_lifecycle_outbox_request_immutable"))
        if self._user_objects() != expected_objects:
            raise LifecycleCommandError("lifecycle database is unrelated or incomplete")
        required_columns = {
            "strategy_lifecycle_schema": {"version"},
            "strategy_instances": {
                "instance_id",
                "incarnation_id",
                "package_id",
                "package_digest",
                "runner_id",
                "state",
                "governance_running",
                "observed_runner_state",
                "supersedes_instance_id",
                "cancellation_status",
                "reconciliation_status",
                "admission_id",
                "created_at",
                "updated_at",
                *(("lifecycle_generation", "core_fence_status") if version >= 2 else ()),
            },
            "strategy_lifecycle_commands": {
                "request_id",
                "fingerprint",
                "result_json",
                "created_at",
            },
            "strategy_lifecycle_audit": {
                "event_id",
                "timestamp",
                "request_id",
                "command",
                "instance_id",
                "actor",
                "result",
                "previous_state",
                "new_state",
            },
        }
        for table, columns in required_columns.items():
            versions = tuple(str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})"))
            if columns != set(versions):
                raise LifecycleCommandError("lifecycle database schema unsupported")
        if version >= 4:
            expected_outbox_columns = {
                "request_id", "instance_id", "incarnation_id", "package_id", "package_digest",
                "expected_lifecycle_generation", "resulting_lifecycle_generation",
                "core_request_json", "core_request_digest", "status", "receipt_json",
                "created_at", "updated_at",
            }
            if version >= 5:
                expected_outbox_columns.add("receipt_sha256")
            actual = {
                str(row[1])
                for row in self._db.execute(
                    "PRAGMA table_info(strategy_lifecycle_core_outbox)"
                )
            }
            if actual != expected_outbox_columns:
                raise LifecycleCommandError("lifecycle database schema unsupported")
        unsafe_rows = self._db.execute(
            "SELECT 1 FROM strategy_instances WHERE governance_running<>0 "
            "OR state='RUNNING' OR admission_id IS NOT NULL LIMIT 1"
        ).fetchone()
        if unsafe_rows is not None:
            raise LifecycleCommandError(
                "lifecycle database contains unverified authorization state"
            )

    def transaction(self) -> sqlite3.Connection:
        self._db.execute("BEGIN IMMEDIATE")
        return self._db

    def close(self) -> None:
        with self._lock:
            self._db.close()


class StrategyLifecycleOwner:
    """Durable Strategy Lifecycle identity and governance owner.

    It deliberately has no execution/cancellation/protection adapters. RESUME remains
    unavailable until authoritative current plan, data, protection, and admission sources
    can be checked both before and at commit.
    """

    def __init__(
        self,
        store: SQLiteStrategyLifecycleStore,
        *,
        audit_approved_for_paper: Callable[[str, str], bool],
        clock: Callable[[], datetime] | None = None,
        core_request_builder: Callable[..., CoreFenceTransition] | None = None,
        core_coordinator: CoreLifecycleCommitCoordinator | None = None,
    ) -> None:
        if not isinstance(store, SQLiteStrategyLifecycleStore):
            raise TypeError("store must be SQLiteStrategyLifecycleStore")
        if not callable(audit_approved_for_paper):
            raise TypeError("audit_approved_for_paper must be callable")
        self.store = store
        self._approval = audit_approved_for_paper
        self._clock = clock or (lambda: datetime.now(UTC))
        if (core_request_builder is None) != (core_coordinator is None):
            raise TypeError("Core request builder and coordinator must be configured together")
        if core_coordinator is not None and not callable(
            getattr(core_coordinator, "commit_transition", None)
        ):
            raise TypeError("core_coordinator must expose commit_transition()")
        self._core_request_builder = core_request_builder
        self._core_coordinator = core_coordinator
        self._reconcile_lock = threading.Lock()

    def create_instance(
        self,
        *,
        package_id: str,
        package_digest: str,
        runner_id: str,
        observed_runner_state: str = "UNKNOWN",
    ) -> StrategyInstance:
        _require_text(package_id, "package_id")
        _require_digest(package_digest)
        _require_text(runner_id, "runner_id")
        _require_text(observed_runner_state, "observed_runner_state")
        if self._approval(package_id, package_digest) is not True:
            raise LifecycleCommandError("strategy lacks independent APPROVED_FOR_PAPER audit")
        now = _iso(self._clock())
        instance_id, incarnation_id = f"si_{uuid.uuid4().hex}", f"inc_{uuid.uuid4().hex}"
        with self.store._lock:
            db = self.store.transaction()
            try:
                db.execute(
                    "INSERT INTO strategy_instances("
                    "instance_id,incarnation_id,package_id,package_digest,runner_id,state,"
                    "governance_running,observed_runner_state,supersedes_instance_id,"
                    "cancellation_status,reconciliation_status,admission_id,created_at,"
                    "updated_at,lifecycle_generation,core_fence_status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        instance_id,
                        incarnation_id,
                        package_id,
                        package_digest,
                        runner_id,
                        StrategyLifecycleState.PENDING_AUTHORIZATION.value,
                        0,
                        observed_runner_state,
                        None,
                        "UNAVAILABLE",
                        "UNAVAILABLE",
                        None,
                        now,
                        now,
                        0,
                        "UNAVAILABLE",
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.get_instance(instance_id)

    def clone_instance(self, instance_id: str, *, runner_id: str) -> StrategyInstance:
        source = self.get_instance(instance_id)
        # Clone preserves immutable package binding, never instance identity or state.
        return self.create_instance(
            package_id=source.package_id,
            package_digest=source.package_digest,
            runner_id=runner_id,
        )

    def supersede(
        self,
        old_instance_id: str,
        *,
        package_id: str,
        package_digest: str,
        runner_id: str,
    ) -> StrategyInstance:
        del old_instance_id, package_id, package_digest, runner_id
        raise LifecycleCommandError(
            "replacement unavailable until canonical order/position flatness and lifecycle "
            "effects have an authoritative atomic owner boundary"
        )

    def command(
        self,
        *,
        request_id: str,
        instance_id: str,
        command: StrategyLifecycleCommand,
        expected_generation: int,
        actor: str,
    ) -> LifecycleCommandResult:
        """CAS against expected Lifecycle generation; Core enforcement remains separate."""
        _require_text(request_id, "request_id")
        _require_text(actor, "actor")
        if not isinstance(command, StrategyLifecycleCommand):
            raise TypeError("command must be StrategyLifecycleCommand")
        _require_generation(expected_generation)
        fingerprint = hashlib.sha256(
            json.dumps(
                [instance_id, command.value, expected_generation, actor], separators=(",", ":")
            ).encode()
        ).hexdigest()
        legacy_fingerprint = hashlib.sha256(
            json.dumps([instance_id, command.value, actor], separators=(",", ":")).encode()
        ).hexdigest()
        now = _iso(self._clock())
        result: LifecycleCommandResult
        with self.store._lock:
            db = self.store.transaction()
            try:
                row = db.execute(
                    "SELECT fingerprint,result_json FROM strategy_lifecycle_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    if row[0] not in {fingerprint, legacy_fingerprint}:
                        raise LifecycleCommandError("request_id idempotency conflict")
                    replay_payload = json.loads(row[1])
                    if replay_payload.get("expected_lifecycle_generation") != expected_generation:
                        raise LifecycleCommandError("request_id idempotency conflict")
                    replayed_instance = _instance_from_mapping(replay_payload["instance"])
                    db.execute("COMMIT")
                    result = LifecycleCommandResult(
                        request_id,
                        command,
                        replayed_instance,
                        True,
                        expected_lifecycle_generation=int(
                            replay_payload["expected_lifecycle_generation"]
                        ),
                        resulting_lifecycle_generation=int(
                            replay_payload["resulting_lifecycle_generation"]
                        ),
                        enforcement_status=str(replay_payload["enforcement_status"]),
                        core_receipt=_transition_receipt_from_mapping(
                            replay_payload.get("core_receipt")
                        ),
                    )
                else:
                    current = self._get(instance_id, db)
                    if current.lifecycle_generation != expected_generation:
                        raise LifecycleGenerationConflict(
                            expected_generation, current.lifecycle_generation
                        )
                    if current.core_fence_status == "PENDING_CORE_COMMIT":
                        raise LifecycleCommandError(
                            "pending Core fence transition must be reconciled before another command"
                        )
                    if command is StrategyLifecycleCommand.RESUME:
                        raise LifecycleCommandError(
                            "RESUME unavailable: authoritative current plan, data, protection, and "
                            "admission sources are absent; commit-time currentness cannot be proven"
                        )
                    if current.state in {
                        StrategyLifecycleState.SUPERSEDED,
                        StrategyLifecycleState.SUSPENDED,
                    }:
                        raise LifecycleCommandError("suspended or superseded instance cannot transition")
                    previous = current.state
                    updated_cursor = db.execute(
                        "UPDATE strategy_instances SET state=?, governance_running=0, "
                        "cancellation_status='UNAVAILABLE', reconciliation_status='UNRESOLVED', "
                        "lifecycle_generation=lifecycle_generation+1, "
                        "core_fence_status='PENDING_CORE_COMMIT', updated_at=? "
                        "WHERE instance_id=? AND lifecycle_generation=?",
                        (
                            StrategyLifecycleState.SUSPEND_PENDING.value,
                            now,
                            instance_id,
                            expected_generation,
                        ),
                    )
                    updated = self._get(instance_id, db)
                    if (
                        updated_cursor.rowcount != 1
                        or updated.lifecycle_generation != expected_generation + 1
                    ):
                        raise LifecycleGenerationConflict(
                            expected_generation, updated.lifecycle_generation
                        )
                    if self._core_request_builder is not None:
                        transition = self._core_request_builder(
                            request_id=request_id,
                            instance=updated,
                            expected_lifecycle_generation=expected_generation,
                            resulting_lifecycle_generation=updated.lifecycle_generation,
                        )
                        _validate_core_transition_binding(transition, request_id, updated)
                        encoded_transition = _core_transition_json(transition)
                        db.execute(
                            "INSERT INTO strategy_lifecycle_core_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                request_id, updated.instance_id, updated.incarnation_id,
                                updated.package_id, updated.package_digest, expected_generation,
                                updated.lifecycle_generation, encoded_transition,
                                transition.request_digest, "PENDING", None, None, now, now,
                            ),
                        )
                    result_json = json.dumps(
                        {
                            "instance": _instance_mapping(updated),
                            "expected_lifecycle_generation": expected_generation,
                            "resulting_lifecycle_generation": updated.lifecycle_generation,
                            "enforcement_status": updated.core_fence_status,
                            "core_receipt": None,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    db.execute(
                        "INSERT INTO strategy_lifecycle_commands VALUES(?,?,?,?)",
                        (request_id, fingerprint, result_json, now),
                    )
                    db.execute(
                        "INSERT INTO strategy_lifecycle_audit(timestamp,request_id,command,instance_id,"
                        "actor,result,previous_state,new_state) VALUES(?,?,?,?,?,?,?,?)",
                        (
                            now,
                            request_id,
                            command.value,
                            instance_id,
                            actor,
                            "PENDING_CORE_COMMIT",
                            previous.value,
                            updated.state.value,
                        ),
                    )
                    db.execute("COMMIT")
                    result = LifecycleCommandResult(
                        request_id, command, updated, False, expected_generation,
                        updated.lifecycle_generation, updated.core_fence_status
                    )
            except BaseException:
                db.execute("ROLLBACK")
                raise
        if command is StrategyLifecycleCommand.SUSPEND and self._core_coordinator is not None:
            return self.reconcile_suspend(request_id, replayed=result.replayed)
        return result

    def reconcile_suspend(
        self, request_id: str, *, replayed: bool = True
    ) -> LifecycleCommandResult:
        """Retry the saved Core transition, then apply only its exact committed receipt."""
        _require_text(request_id, "request_id")
        if self._core_request_builder is None or self._core_coordinator is None:
            return self._saved_command_result(request_id, replayed=replayed)
        with self._reconcile_lock:
            transition: CoreFenceTransition
            with self.store._lock:
                db = self.store.transaction()
                try:
                    row = db.execute(
                        "SELECT result_json FROM strategy_lifecycle_commands WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    if row is None:
                        raise LifecycleCommandError("suspend request is unavailable")
                    saved = json.loads(row[0])
                    instance = _instance_from_mapping(saved.get("instance"))
                    if instance.state is StrategyLifecycleState.SUSPENDED:
                        db.execute("COMMIT")
                        return self._saved_command_result(request_id, replayed=replayed)
                    if (
                        instance.state is not StrategyLifecycleState.SUSPEND_PENDING
                        or instance.core_fence_status != "PENDING_CORE_COMMIT"
                    ):
                        raise LifecycleCommandError("request has no pending Core suspend transition")
                    outbox = db.execute(
                        "SELECT core_request_json,core_request_digest,status,receipt_json,receipt_sha256,"
                        "instance_id,incarnation_id,package_id,package_digest,"
                        "expected_lifecycle_generation,resulting_lifecycle_generation "
                        "FROM strategy_lifecycle_core_outbox WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    if outbox is None:
                        db.execute("COMMIT")
                        return self._saved_command_result(request_id, replayed=replayed)
                    else:
                        encoded = str(outbox[0])
                        transition = _core_transition_from_json(encoded)
                        if (
                            encoded != _core_transition_json(transition)
                            or transition.request_digest != outbox[1]
                            or (
                                str(outbox[5]), str(outbox[6]), str(outbox[7]), str(outbox[8]),
                                int(outbox[9]), int(outbox[10]),
                            ) != (
                                instance.instance_id, instance.incarnation_id, instance.package_id,
                                instance.package_digest,
                                int(saved["expected_lifecycle_generation"]),
                                int(saved["resulting_lifecycle_generation"]),
                            )
                        ):
                            raise LifecycleCommandError("saved Core transition digest is invalid")
                        _validate_core_transition_binding(transition, request_id, instance)
                        if outbox[2] == "ENFORCED":
                            receipt = _transition_receipt_from_json(str(outbox[3]))
                            if (
                                receipt is None
                                or _sha256_text(str(outbox[3])) != outbox[4]
                                or not receipt.matches_request(transition)
                            ):
                                raise LifecycleCommandError("saved Core receipt does not match request")
                            db.execute("COMMIT")
                            return self._saved_command_result(request_id, replayed=replayed)
                        db.execute("COMMIT")
                except BaseException:
                    if db.in_transaction:
                        db.execute("ROLLBACK")
                    raise

            # Core is called only after the lifecycle transaction committed.
            try:
                result = self._core_coordinator.commit_transition(transition)
            except Exception:
                return self._saved_command_result(request_id, replayed=replayed)
            if (
                type(result) is not CoreLifecycleCommitResult
                or result.status is not CoreCommitStatus.COMMITTED
                or type(result.receipt) is not CoreFenceTransitionReceipt
                or not result.receipt.matches_request(transition)
            ):
                return self._saved_command_result(request_id, replayed=replayed)
            return self._apply_core_confirmation(
                request_id, transition, result.receipt, replayed=replayed
            )

    def _apply_core_confirmation(
        self,
        request_id: str,
        transition: CoreFenceTransition,
        receipt: CoreFenceTransitionReceipt,
        *,
        replayed: bool,
    ) -> LifecycleCommandResult:
        if not receipt.matches_request(transition):
            return self._saved_command_result(request_id, replayed=replayed)
        with self.store._lock:
            db = self.store.transaction()
            try:
                row = db.execute(
                    "SELECT core_request_json,core_request_digest,status,receipt_json,receipt_sha256,"
                    "instance_id,incarnation_id,package_id,package_digest,resulting_lifecycle_generation "
                    "FROM strategy_lifecycle_core_outbox WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is None or str(row[1]) != transition.request_digest:
                    raise LifecycleCommandError("Core outbox changed during reconciliation")
                if str(row[0]) != _core_transition_json(transition):
                    raise LifecycleCommandError("Core outbox request changed during reconciliation")
                if row[2] == "ENFORCED":
                    prior = _transition_receipt_from_json(str(row[3]))
                    if (
                        prior is None
                        or _sha256_text(str(row[3])) != row[4]
                        or not prior.same_commit(receipt)
                    ):
                        raise LifecycleCommandError("Core receipt conflicts with saved enforcement")
                    db.execute("COMMIT")
                    return self._saved_command_result(request_id, replayed=replayed)
                instance = self._get(str(row[5]), db)
                if (
                    instance.state is not StrategyLifecycleState.SUSPEND_PENDING
                    or instance.core_fence_status != "PENDING_CORE_COMMIT"
                    or instance.instance_id != str(row[5])
                    or instance.incarnation_id != str(row[6])
                    or instance.package_id != str(row[7])
                    or instance.package_digest != str(row[8])
                    or instance.lifecycle_generation != int(row[9])
                    or transition.lifecycle.instance_id != str(row[5])
                    or transition.lifecycle.incarnation_id != str(row[6])
                    or transition.lifecycle.package_id != str(row[7])
                    or transition.lifecycle.package_digest != str(row[8])
                ):
                    raise LifecycleCommandError("Lifecycle state changed before Core confirmation")
                now = _iso(self._clock())
                receipt_json = _core_receipt_json(receipt)
                receipt_sha256 = _sha256_text(receipt_json)
                outbox_update = db.execute(
                    "UPDATE strategy_lifecycle_core_outbox SET status='ENFORCED',receipt_json=?,receipt_sha256=?,"
                    "updated_at=? WHERE request_id=? AND status='PENDING' AND receipt_json IS NULL "
                    "AND receipt_sha256 IS NULL",
                    (receipt_json, receipt_sha256, now, request_id),
                )
                if outbox_update.rowcount != 1:
                    raise LifecycleCommandError("Core outbox compare-and-set failed")
                instance_update = db.execute(
                    "UPDATE strategy_instances SET state=?,core_fence_status='ENFORCED',"
                    "reconciliation_status='CORE_COMMITTED',updated_at=? "
                    "WHERE instance_id=? AND incarnation_id=? AND package_id=? AND package_digest=? "
                    "AND lifecycle_generation=? AND state='SUSPEND_PENDING' "
                    "AND core_fence_status='PENDING_CORE_COMMIT'",
                    (StrategyLifecycleState.SUSPENDED.value, now, instance.instance_id,
                     instance.incarnation_id, instance.package_id, instance.package_digest,
                     int(row[9])),
                )
                if instance_update.rowcount != 1:
                    raise LifecycleCommandError("Lifecycle compare-and-set failed on Core confirmation")
                updated = self._get(instance.instance_id, db)
                command_row = db.execute(
                    "SELECT result_json FROM strategy_lifecycle_commands WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                payload = json.loads(command_row[0])
                payload.update(
                    {
                        "instance": _instance_mapping(updated),
                        "enforcement_status": "ENFORCED",
                        "core_receipt": json.loads(receipt_json),
                    }
                )
                db.execute(
                    "UPDATE strategy_lifecycle_commands SET result_json=? WHERE request_id=?",
                    (json.dumps(payload, sort_keys=True, separators=(",", ":")), request_id),
                )
                db.execute(
                    "INSERT INTO strategy_lifecycle_audit(timestamp,request_id,command,instance_id,"
                    "actor,result,previous_state,new_state) VALUES(?,?,?,?,?,?,?,?)",
                    (now, request_id, "CORE_SUSPEND_CONFIRMED", instance.instance_id,
                     "core-reconciler", "ENFORCED", instance.state.value,
                     StrategyLifecycleState.SUSPENDED.value),
                )
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                return self._saved_command_result(request_id, replayed=replayed)
        return self._saved_command_result(request_id, replayed=replayed)

    def _saved_command_result(self, request_id: str, *, replayed: bool) -> LifecycleCommandResult:
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT result_json FROM strategy_lifecycle_commands WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise LifecycleCommandError("lifecycle command is unavailable")
            payload = json.loads(row[0])
            outbox = self.store._db.execute(
                "SELECT core_request_json,status,receipt_json,receipt_sha256 "
                "FROM strategy_lifecycle_core_outbox WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if outbox is not None and outbox[1] == "ENFORCED":
                request = _core_transition_from_json(str(outbox[0]))
                receipt_json = str(outbox[2])
                receipt = _transition_receipt_from_json(receipt_json)
                if (
                    receipt is None
                    or _sha256_text(receipt_json) != outbox[3]
                    or not receipt.matches_request(request)
                    or payload.get("core_receipt") != json.loads(receipt_json)
                    or payload.get("enforcement_status") != "ENFORCED"
                ):
                    raise LifecycleCommandError("saved Core receipt integrity check failed")
            return LifecycleCommandResult(
                request_id,
                StrategyLifecycleCommand.SUSPEND,
                _instance_from_mapping(payload["instance"]),
                replayed,
                int(payload["expected_lifecycle_generation"]),
                int(payload["resulting_lifecycle_generation"]),
                str(payload["enforcement_status"]),
                _transition_receipt_from_mapping(payload.get("core_receipt")),
            )

    def observe_runner_state(self, instance_id: str, state: str) -> StrategyInstance:
        _require_text(state, "runner_state")
        with self.store._lock:
            db = self.store.transaction()
            try:
                cursor = db.execute(
                    "UPDATE strategy_instances SET observed_runner_state=?, updated_at=? WHERE instance_id=?",
                    (state, _iso(self._clock()), instance_id),
                )
                if cursor.rowcount == 0:
                    raise LifecycleCommandError("strategy instance not found")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return self.get_instance(instance_id)

    def get_instance(self, instance_id: str) -> StrategyInstance:
        with self.store._lock:
            return self._get(instance_id, self.store._db)

    def _get(self, instance_id: str, db: sqlite3.Connection) -> StrategyInstance:
        row = db.execute(
            "SELECT instance_id,incarnation_id,package_id,package_digest,runner_id,state,"
            "governance_running,observed_runner_state,supersedes_instance_id,cancellation_status,"
            "reconciliation_status,admission_id,lifecycle_generation,core_fence_status "
            "FROM strategy_instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            raise LifecycleCommandError("strategy instance not found")
        return StrategyInstance(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            StrategyLifecycleState(row[5]),
            bool(row[6]),
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
            row[12],
            row[13],
        )


class StrategyLifecycleService:
    """Typed application boundary; does not imply effect execution availability."""

    def __init__(self, owner: StrategyLifecycleOwner) -> None:
        if not isinstance(owner, StrategyLifecycleOwner):
            raise TypeError("owner must be StrategyLifecycleOwner")
        self.owner = owner

    def create_instance(
        self,
        *,
        package_id: str,
        package_digest: str,
        runner_id: str,
    ) -> StrategyInstance:
        return self.owner.create_instance(
            package_id=package_id,
            package_digest=package_digest,
            runner_id=runner_id,
        )

    def clone_instance(self, instance_id: str, *, runner_id: str) -> StrategyInstance:
        return self.owner.clone_instance(instance_id, runner_id=runner_id)

    def supersede(
        self,
        old_instance_id: str,
        *,
        package_id: str,
        package_digest: str,
        runner_id: str,
    ) -> StrategyInstance:
        return self.owner.supersede(
            old_instance_id,
            package_id=package_id,
            package_digest=package_digest,
            runner_id=runner_id,
        )

    def suspend(
        self, request_id: str, instance_id: str, expected_generation: int, actor: str
    ) -> LifecycleCommandResult:
        return self.owner.command(
            request_id=request_id,
            instance_id=instance_id,
            command=StrategyLifecycleCommand.SUSPEND,
            expected_generation=expected_generation,
            actor=actor,
        )

    def resume(
        self, request_id: str, instance_id: str, expected_generation: int, actor: str
    ) -> LifecycleCommandResult:
        return self.owner.command(
            request_id=request_id,
            instance_id=instance_id,
            command=StrategyLifecycleCommand.RESUME,
            expected_generation=expected_generation,
            actor=actor,
        )


def _instance_mapping(instance: StrategyInstance) -> dict[str, object]:
    return {
        "instance_id": instance.instance_id,
        "incarnation_id": instance.incarnation_id,
        "package_id": instance.package_id,
        "package_digest": instance.package_digest,
        "runner_id": instance.runner_id,
        "state": instance.state.value,
        "governance_running": instance.governance_running,
        "observed_runner_state": instance.observed_runner_state,
        "supersedes_instance_id": instance.supersedes_instance_id,
        "cancellation_status": instance.cancellation_status,
        "reconciliation_status": instance.reconciliation_status,
        "admission_id": instance.admission_id,
        "lifecycle_generation": instance.lifecycle_generation,
        "core_fence_status": instance.core_fence_status,
    }


def _validate_core_transition_binding(
    request: CoreFenceTransition, request_id: str, instance: StrategyInstance
) -> None:
    if type(request) is not CoreFenceTransition:
        raise LifecycleCommandError("trusted Core request builder returned an invalid transition")
    claim = request.lifecycle
    if (
        request.transition_id != request_id
        or request.action is not CoreLifecycleAction.SUSPEND
        or (claim.instance_id, claim.incarnation_id, claim.package_id, claim.package_digest)
        != (instance.instance_id, instance.incarnation_id, instance.package_id, instance.package_digest)
    ):
        raise LifecycleCommandError("Core transition does not match immutable lifecycle binding")


def _core_transition_json(request: CoreFenceTransition) -> str:
    claim = request.lifecycle
    value = {
        "transition_id": request.transition_id,
        "instance": {
            "instance_id": claim.instance_id,
            "incarnation_id": claim.incarnation_id,
            "package_id": claim.package_id,
            "package_digest": claim.package_digest,
            "expected_core_generation": claim.expected_generation,
        },
        "writer": {"owner_id": request.writer.owner_id, "epoch": request.writer.epoch},
        "resulting_core_generation": request.resulting_generation,
        "action": request.action.value,
        "canonical_payload_digest": request.canonical_payload_digest,
        "canonical_payload_b64": base64.b64encode(request.canonical_payload).decode("ascii"),
        "authority_refs": [
            {"authority": item.authority, "reference_id": item.reference_id, "digest": item.digest}
            for item in request.authority_refs
        ],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _core_transition_from_json(encoded: str) -> CoreFenceTransition:
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise LifecycleCommandError("saved Core transition is malformed")
    try:
        claim = value["instance"]
        if not isinstance(claim, dict):
            raise TypeError
        payload = base64.b64decode(value["canonical_payload_b64"], validate=True)
        return CoreFenceTransition(
            str(value["transition_id"]),
            CoreLifecycleExpectedGeneration(
                str(claim["instance_id"]), str(claim["incarnation_id"]),
                str(claim["package_id"]), str(claim["package_digest"]),
                int(claim["expected_core_generation"]),
            ),
            CoreWriterIdentity(str(value["writer"]["owner_id"]), int(value["writer"]["epoch"])),
            int(value["resulting_core_generation"]),
            CoreLifecycleAction(value["action"]),
            str(value["canonical_payload_digest"]),
            payload,
            tuple(
                CoreAuthorityReference(str(item["authority"]), str(item["reference_id"]), str(item["digest"]))
                for item in value["authority_refs"]
            ),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LifecycleCommandError("saved Core transition is malformed") from exc


def _core_receipt_json(receipt: CoreFenceTransitionReceipt) -> str:
    return json.dumps(
        {
            "transition_id": receipt.transition_id,
            "instance_id": receipt.instance_id,
            "incarnation_id": receipt.incarnation_id,
            "package_id": receipt.package_id,
            "package_digest": receipt.package_digest,
            "writer": {"owner_id": receipt.writer.owner_id, "epoch": receipt.writer.epoch},
            "previous_generation": receipt.previous_generation,
            "resulting_generation": receipt.resulting_generation,
            "action": receipt.action.value,
            "canonical_payload_digest": receipt.canonical_payload_digest,
            "authority_refs": [
                {"authority": item.authority, "reference_id": item.reference_id, "digest": item.digest}
                for item in receipt.authority_refs
            ],
            "canonical_commit_sequence": receipt.canonical_commit_sequence,
            "committed_at": receipt.committed_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _transition_receipt_from_json(encoded: str) -> CoreFenceTransitionReceipt | None:
    value = json.loads(encoded)
    return _transition_receipt_from_mapping(value)


def _transition_receipt_from_mapping(value: object) -> CoreFenceTransitionReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LifecycleCommandError("saved Core receipt is malformed")
    try:
        return CoreFenceTransitionReceipt(
            str(value["transition_id"]), str(value["instance_id"]),
            str(value["incarnation_id"]), str(value["package_id"]),
            str(value["package_digest"]),
            CoreWriterIdentity(str(value["writer"]["owner_id"]), int(value["writer"]["epoch"])),
            int(value["previous_generation"]), int(value["resulting_generation"]),
            CoreLifecycleAction(value["action"]), str(value["canonical_payload_digest"]),
            tuple(
                CoreAuthorityReference(str(item["authority"]), str(item["reference_id"]), str(item["digest"]))
                for item in value["authority_refs"]
            ),
            int(value["canonical_commit_sequence"]),
            datetime.fromisoformat(str(value["committed_at"])),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise LifecycleCommandError("saved Core receipt is malformed") from exc


def _instance_from_mapping(value: object) -> StrategyInstance:
    if not isinstance(value, dict):
        raise LifecycleCommandError("persisted lifecycle command result is invalid")
    try:
        return StrategyInstance(
            instance_id=str(value["instance_id"]),
            incarnation_id=str(value["incarnation_id"]),
            package_id=str(value["package_id"]),
            package_digest=str(value["package_digest"]),
            runner_id=str(value["runner_id"]),
            state=StrategyLifecycleState(str(value["state"])),
            governance_running=value["governance_running"] is True,
            observed_runner_state=str(value["observed_runner_state"]),
            supersedes_instance_id=(
                str(value["supersedes_instance_id"])
                if value["supersedes_instance_id"] is not None
                else None
            ),
            cancellation_status=str(value["cancellation_status"]),
            reconciliation_status=str(value["reconciliation_status"]),
            admission_id=(str(value["admission_id"]) if value["admission_id"] else None),
            lifecycle_generation=int(value["lifecycle_generation"]),
            core_fence_status=str(value["core_fence_status"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleCommandError("persisted lifecycle command result is invalid") from exc


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise LifecycleCommandError(f"{name} must be non-blank trimmed text")


def _require_generation(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LifecycleCommandError("generation must be a non-negative integer")


def _require_aware_time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


def _require_digest(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise LifecycleCommandError("package_digest must be lowercase SHA-256")


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleCommandError("owner clock must return timezone-aware datetime")
    return value.astimezone(UTC).isoformat()
