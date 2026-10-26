from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    SseAdmissionLimiter,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import (
    AuditContext,
    AuditIntegrityError,
    ControlPlaneStateError,
    SQLiteControlPlaneStore,
)
from market_data_application.errors import ApiAuthenticationError, ApiAuthorizationError
from market_data_application.security import ApiRole

NOW = datetime(2026, 7, 29, 19, 0, tzinfo=UTC)
OLD_ADMIN_TOKEN = "old-admin-token-000000000000000000000000"
NEW_ADMIN_TOKEN = "new-admin-token-000000000000000000000000"
READER_TOKEN = "reader-token-000000000000000000000000000"
AUDIT_KEY = b"audit-key-for-tests-must-be-at-least-32-bytes"


def _record(
    token_id: str,
    token: str,
    role: ApiRole,
    *,
    not_before: datetime = NOW,
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> ApiTokenRecord:
    return ApiTokenRecord.from_plaintext(
        token_id=token_id,
        token=token,
        role=role,
        not_before=not_before,
        expires_at=expires_at,
        revoked=revoked,
    )


def _snapshot(
    generation: int,
    *records: ApiTokenRecord,
    created_at: datetime = NOW,
) -> ApiTokenSnapshot:
    return ApiTokenSnapshot(
        generation=generation,
        created_at=created_at,
        records=tuple(records),
    )


def _audit(
    action: str,
    *,
    outcome: str = "accepted",
    timestamp: datetime = NOW,
    details: dict[str, object] | None = None,
) -> AuditContext:
    return AuditContext(
        timestamp=timestamp,
        correlation_id="corr-security-test",
        direct_peer="127.0.0.1",
        principal_id="admin-primary",
        token_id="admin-primary",
        action=action,
        outcome=outcome,
        details=details or {},
    )


def test_token_record_mapping_contains_digest_but_not_plaintext() -> None:
    record = _record("admin-primary", OLD_ADMIN_TOKEN, ApiRole.ADMIN)

    mapping = record.to_mapping()

    assert mapping["token_id"] == "admin-primary"
    assert mapping["role"] == "admin"
    assert mapping["digest_sha256"] != OLD_ADMIN_TOKEN
    assert OLD_ADMIN_TOKEN not in repr(mapping)
    assert ApiTokenRecord.from_mapping(mapping) == record


def test_snapshot_rejects_duplicate_ids_and_digests() -> None:
    first = _record("admin-primary", OLD_ADMIN_TOKEN, ApiRole.ADMIN)
    duplicate_id = _record("admin-primary", NEW_ADMIN_TOKEN, ApiRole.ADMIN)
    duplicate_digest = _record("admin-secondary", OLD_ADMIN_TOKEN, ApiRole.ADMIN)

    with pytest.raises(ValueError, match="Token IDs|token IDs"):
        _snapshot(1, first, duplicate_id)
    with pytest.raises(ValueError, match="digests"):
        _snapshot(1, first, duplicate_digest)


def test_authenticator_rotation_overlap_expiry_and_revocation() -> None:
    current_time = [NOW]
    old = _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN)
    initial = _snapshot(1, old)
    authenticator = ReloadableApiTokenAuthenticator(
        TokenSnapshotState(current=initial),
        clock=lambda: current_time[0],
    )

    old_principal = authenticator.authenticate(OLD_ADMIN_TOKEN, required_role=ApiRole.ADMIN)
    assert old_principal.token_id == "admin-old"
    assert old_principal.token_generation == 1

    new = _record("admin-new", NEW_ADMIN_TOKEN, ApiRole.ADMIN)
    rotated = _snapshot(2, new, created_at=NOW + timedelta(seconds=1))
    state = authenticator.reload(rotated, overlap_seconds=30, now=NOW + timedelta(seconds=1))
    assert state.previous is initial

    current_time[0] = NOW + timedelta(seconds=20)
    assert (
        authenticator.authenticate(OLD_ADMIN_TOKEN, required_role=ApiRole.READER).token_generation
        == 1
    )
    assert (
        authenticator.authenticate(NEW_ADMIN_TOKEN, required_role=ApiRole.ADMIN).token_generation
        == 2
    )

    current_time[0] = NOW + timedelta(seconds=32)
    with pytest.raises(ApiAuthenticationError):
        authenticator.authenticate(OLD_ADMIN_TOKEN, required_role=ApiRole.READER)

    tombstone = _record(
        "admin-old",
        OLD_ADMIN_TOKEN,
        ApiRole.ADMIN,
        revoked=True,
    )
    third = _snapshot(
        3,
        new,
        tombstone,
        created_at=NOW + timedelta(seconds=33),
    )
    authenticator.reload(third, overlap_seconds=30, now=NOW + timedelta(seconds=33))
    current_time[0] = NOW + timedelta(seconds=34)
    with pytest.raises(ApiAuthenticationError):
        authenticator.authenticate(OLD_ADMIN_TOKEN, required_role=ApiRole.READER)


def test_authenticator_enforces_role_and_rejects_rollback() -> None:
    reader = _record("reader", READER_TOKEN, ApiRole.READER)
    snapshot = _snapshot(5, reader)
    authenticator = ReloadableApiTokenAuthenticator(TokenSnapshotState(current=snapshot))

    with pytest.raises(ApiAuthorizationError):
        authenticator.authenticate(READER_TOKEN, required_role=ApiRole.OPERATOR)
    with pytest.raises(ValueError, match="increase monotonically"):
        authenticator.reload(_snapshot(5, reader), overlap_seconds=0)
    with pytest.raises(ValueError, match="increase monotonically"):
        authenticator.reload(_snapshot(4, reader), overlap_seconds=0)


def test_token_activation_and_expiry_are_fail_closed() -> None:
    future = _record(
        "future-reader",
        READER_TOKEN,
        ApiRole.READER,
        not_before=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=2),
    )
    current_time = [NOW]
    authenticator = ReloadableApiTokenAuthenticator(
        TokenSnapshotState(current=_snapshot(1, future)),
        clock=lambda: current_time[0],
    )

    with pytest.raises(ApiAuthenticationError):
        authenticator.authenticate(READER_TOKEN, required_role=ApiRole.READER)
    current_time[0] = NOW + timedelta(seconds=90)
    assert authenticator.authenticate(READER_TOKEN, required_role=ApiRole.READER)
    current_time[0] = NOW + timedelta(minutes=2)
    with pytest.raises(ApiAuthenticationError):
        authenticator.authenticate(READER_TOKEN, required_role=ApiRole.READER)


def test_bounded_token_bucket_recovers_and_evicts_oldest_key() -> None:
    current_time = [100.0]
    limiter = BoundedTokenBucketLimiter(
        capacity=2,
        refill_per_second=1.0,
        max_entries=2,
        clock=lambda: current_time[0],
    )

    assert limiter.consume("principal:a").allowed is True
    assert limiter.consume("principal:a").allowed is True
    denied = limiter.consume("principal:a")
    assert denied.allowed is False
    assert denied.retry_after_seconds == 1

    current_time[0] += 1.0
    assert limiter.consume("principal:a").allowed is True
    limiter.consume("principal:b")
    limiter.consume("principal:c")
    assert limiter.entry_count == 2

    current_time[0] += 1.0
    # The oldest key was evicted, so a recreated bucket starts at full capacity.
    recreated = limiter.consume("principal:a")
    assert recreated.allowed is True
    assert recreated.remaining == 1.0


def test_sse_admission_is_bounded_and_released() -> None:
    limiter = SseAdmissionLimiter(
        ControlPlaneLimits(
            max_sse_global=2,
            max_sse_per_principal=1,
            max_sse_per_peer=2,
        )
    )

    first = limiter.acquire("reader-a", "127.0.0.1")
    with pytest.raises(RuntimeError, match="principal"):
        limiter.acquire("reader-a", "127.0.0.1")
    second = limiter.acquire("reader-b", "127.0.0.1")
    with pytest.raises(RuntimeError, match="global"):
        limiter.acquire("reader-c", "::1")

    first.release()
    replacement = limiter.acquire("reader-c", "::1")
    replacement.release()
    second.release()


def test_store_bootstrap_replace_restart_and_audit_chain(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.sqlite3"
    first = TokenSnapshotState(
        current=_snapshot(1, _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN))
    )
    store = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)

    assert store.bootstrap(first, audit=_audit("security.bootstrap")) == first
    assert store.bootstrap(first, audit=_audit("security.bootstrap")) == first

    second_snapshot = _snapshot(
        2,
        _record("admin-new", NEW_ADMIN_TOKEN, ApiRole.ADMIN),
        created_at=NOW + timedelta(seconds=1),
    )
    second = TokenSnapshotState(
        current=second_snapshot,
        previous=first.current,
        previous_expires_at=NOW + timedelta(seconds=31),
    )
    store.replace_state(
        second,
        expected_generation=1,
        audit=_audit(
            "security.reload",
            timestamp=NOW + timedelta(seconds=1),
            details={"generation": 2, "record_count": 1, "overlap_seconds": 30},
        ),
    )

    status = store.verify_audit_chain()
    assert status.valid is True
    assert status.record_count == 2
    assert status.head_mac != "0" * 64
    assert store.load_state() == second

    restarted = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)
    assert restarted.load_state() == second
    assert restarted.verify_audit_chain() == status
    records = restarted.audit_records()
    assert [record.action for record in records] == [
        "security.bootstrap",
        "security.reload",
    ]


def test_store_rejects_generation_rollback_and_compare_and_swap(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.sqlite3"
    first = TokenSnapshotState(
        current=_snapshot(3, _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN))
    )
    store = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)
    store.bootstrap(first, audit=_audit("security.bootstrap"))

    with pytest.raises(ControlPlaneStateError, match="roll back"):
        store.bootstrap(
            TokenSnapshotState(
                current=_snapshot(2, _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN))
            ),
            audit=_audit("security.bootstrap"),
        )

    next_state = TokenSnapshotState(
        current=_snapshot(4, _record("admin-new", NEW_ADMIN_TOKEN, ApiRole.ADMIN))
    )
    with pytest.raises(ControlPlaneStateError, match="changed"):
        store.replace_state(
            next_state,
            expected_generation=2,
            audit=_audit("security.reload"),
        )


def test_store_detects_audit_and_state_tampering(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.sqlite3"
    state = TokenSnapshotState(
        current=_snapshot(1, _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN))
    )
    store = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)
    store.bootstrap(state, audit=_audit("security.bootstrap"))

    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE control_plane_audit SET outcome = 'forged' WHERE sequence = 1")
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="MAC"):
        SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)

    second_path = tmp_path / "control-plane-state.sqlite3"
    second_store = SQLiteControlPlaneStore(second_path, audit_key=AUDIT_KEY)
    second_store.bootstrap(state, audit=_audit("security.bootstrap"))
    with closing(sqlite3.connect(second_path)) as connection:
        connection.execute("UPDATE control_plane_state SET state_json = '{}' WHERE singleton = 1")
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="fingerprint"):
        SQLiteControlPlaneStore(second_path, audit_key=AUDIT_KEY)


def test_store_never_persists_plaintext_token(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.sqlite3"
    state = TokenSnapshotState(
        current=_snapshot(1, _record("admin-old", OLD_ADMIN_TOKEN, ApiRole.ADMIN))
    )
    store = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)
    store.bootstrap(state, audit=_audit("security.bootstrap"))

    persisted = b"".join(
        candidate.read_bytes() for candidate in sorted(tmp_path.iterdir()) if candidate.is_file()
    )
    assert OLD_ADMIN_TOKEN.encode("utf-8") not in persisted


def test_audit_details_reject_secret_bearing_keys(tmp_path: Path) -> None:
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3", audit_key=AUDIT_KEY)

    with pytest.raises(ValueError, match="forbidden"):
        store.append_audit(_audit("security.failure", details={"token": OLD_ADMIN_TOKEN}))
