from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
    _state_from_mapping,
)
from market_data_application.security import ApiRole

NOW = datetime(2026, 7, 29, 19, 0, tzinfo=UTC)
TOKEN_A = "edge-admin-token-000000000000000000000000"
TOKEN_B = "edge-reader-token-00000000000000000000000"
AUDIT_KEY = b"edge-audit-key-must-contain-at-least-32-bytes"


def _record(
    token_id: str = "admin-edge",
    token: str = TOKEN_A,
    role: ApiRole = ApiRole.ADMIN,
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


def _snapshot(generation: int = 1, *records: ApiTokenRecord) -> ApiTokenSnapshot:
    return ApiTokenSnapshot(
        generation=generation,
        created_at=NOW,
        records=records or (_record(),),
    )


def _state(generation: int = 1) -> TokenSnapshotState:
    return TokenSnapshotState(current=_snapshot(generation))


def _audit(**changes: object) -> AuditContext:
    values: dict[str, object] = {
        "timestamp": NOW,
        "correlation_id": "corr-edge",
        "direct_peer": "127.0.0.1",
        "principal_id": "admin-edge",
        "token_id": "admin-edge",
        "action": "security.edge",
        "outcome": "accepted",
        "details": {"generation": 1},
    }
    values.update(changes)
    return AuditContext(**values)  # type: ignore[arg-type]


def _fingerprint(serialized: str) -> str:
    return sha256(serialized.encode("utf-8")).hexdigest()


def test_token_record_rejects_invalid_identity_digest_and_timestamps() -> None:
    digest = b"x" * 32
    with pytest.raises(ValueError, match="token_id"):
        ApiTokenRecord("", digest, ApiRole.ADMIN, NOW)
    with pytest.raises(ValueError, match="32 bytes"):
        ApiTokenRecord("admin-edge", b"short", ApiRole.ADMIN, NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        ApiTokenRecord("admin-edge", digest, ApiRole.ADMIN, datetime(2026, 7, 29))
    with pytest.raises(ValueError, match="after activation"):
        ApiTokenRecord("admin-edge", digest, ApiRole.ADMIN, NOW, expires_at=NOW)


def test_token_record_mapping_is_strictly_typed() -> None:
    valid = _record().to_mapping()
    invalid_values = (
        ("digest_sha256", "A" * 64, "digest_sha256"),
        ("token_id", 7, "token_id"),
        ("role", 7, "role"),
        ("not_before", 7, "not_before"),
        ("expires_at", 7, "expires_at"),
        ("revoked", "false", "revoked"),
    )
    for key, value, match in invalid_values:
        candidate = dict(valid)
        candidate[key] = value
        with pytest.raises(ValueError, match=match):
            ApiTokenRecord.from_mapping(candidate)


def test_snapshot_mapping_revocation_and_state_validation_edges() -> None:
    record = _record()
    with pytest.raises(ValueError, match="positive"):
        ApiTokenSnapshot(generation=0, created_at=NOW, records=(record,))
    with pytest.raises(ValueError, match="at least one"):
        ApiTokenSnapshot(generation=1, created_at=NOW, records=())

    valid = _snapshot().to_mapping()
    for key, value, match in (
        ("generation", True, "generation"),
        ("created_at", 7, "created_at"),
        ("records", {}, "records"),
    ):
        candidate = dict(valid)
        candidate[key] = value
        with pytest.raises(ValueError, match=match):
            ApiTokenSnapshot.from_mapping(candidate)

    with pytest.raises(KeyError):
        _snapshot().with_revoked("missing", generation=2, now=NOW)
    revoked = _snapshot().with_revoked("admin-edge", generation=2, now=NOW)
    assert revoked.records[0].revoked is True

    with pytest.raises(ValueError, match="configured together"):
        TokenSnapshotState(current=_snapshot(2), previous=_snapshot(1))
    with pytest.raises(ValueError, match="lower"):
        TokenSnapshotState(
            current=_snapshot(2),
            previous=_snapshot(2),
            previous_expires_at=NOW + timedelta(seconds=1),
        )


def test_authenticator_state_reload_and_replacement_edges() -> None:
    authenticator = ReloadableApiTokenAuthenticator(_state(2), clock=lambda: NOW)
    assert authenticator.state.current.generation == 2
    with pytest.raises(ValueError, match="between 0 and 3600"):
        authenticator.reload(_snapshot(3), overlap_seconds=-1)
    with pytest.raises(ValueError, match="between 0 and 3600"):
        authenticator.reload(_snapshot(3), overlap_seconds=3601)
    with pytest.raises(ValueError, match="older"):
        authenticator.replace_state(_state(1))
    authenticator.replace_state(_state(2))
    assert authenticator.state.current.generation == 2


def test_rate_limiter_and_control_limits_reject_invalid_configuration() -> None:
    for arguments, match in (
        ({"capacity": 0, "refill_per_second": 1.0, "max_entries": 1}, "capacity"),
        ({"capacity": 1, "refill_per_second": 0.0, "max_entries": 1}, "refill"),
        ({"capacity": 1, "refill_per_second": 1.0, "max_entries": 0}, "max_entries"),
    ):
        with pytest.raises(ValueError, match=match):
            BoundedTokenBucketLimiter(**arguments)

    limiter = BoundedTokenBucketLimiter(capacity=2, refill_per_second=1.0, max_entries=2)
    with pytest.raises(ValueError, match="key"):
        limiter.consume("")
    with pytest.raises(ValueError, match="key"):
        limiter.consume("x" * 513)
    with pytest.raises(ValueError, match="amount"):
        limiter.consume("edge", amount=0)
    with pytest.raises(ValueError, match="amount"):
        limiter.consume("edge", amount=3)
    with pytest.raises(ValueError, match="positive"):
        ControlPlaneLimits(max_header_bytes=0)


def test_sse_peer_context_double_release_and_underflow_edges() -> None:
    peer_limiter = SseAdmissionLimiter(
        ControlPlaneLimits(max_sse_global=4, max_sse_per_principal=4, max_sse_per_peer=1)
    )
    lease = peer_limiter.acquire("reader-a", "127.0.0.1")
    with pytest.raises(RuntimeError, match="peer"):
        peer_limiter.acquire("reader-b", "127.0.0.1")
    lease.release()
    lease.release()

    limiter = SseAdmissionLimiter(
        ControlPlaneLimits(max_sse_global=4, max_sse_per_principal=4, max_sse_per_peer=4)
    )
    with pytest.raises(RuntimeError, match="state underflow"):
        limiter.release("missing", "missing")

    first = limiter.acquire("reader", "::1")
    second = limiter.acquire("reader", "::1")
    with first:
        pass
    second.release()

    held = limiter.acquire("reader", "::1")
    with pytest.raises(RuntimeError, match="key underflow"):
        limiter.release("other", "::1")
    held.release()

    with limiter:
        pass


def test_store_rejects_short_key_uninitialized_state_and_bootstrap_mismatch(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        SQLiteControlPlaneStore(tmp_path / "short.sqlite3", audit_key=b"short")

    store = SQLiteControlPlaneStore(tmp_path / "edge.sqlite3", audit_key=AUDIT_KEY)
    with pytest.raises(ControlPlaneStateError, match="not initialized"):
        store.load_state()

    store.bootstrap(_state(1), audit=_audit())
    different = TokenSnapshotState(
        current=_snapshot(1, _record("reader-edge", TOKEN_B, ApiRole.READER))
    )
    with pytest.raises(ControlPlaneStateError, match="differs"):
        store.bootstrap(different, audit=_audit())
    with pytest.raises(ControlPlaneStateError, match="increase monotonically"):
        store.replace_state(_state(1), expected_generation=1, audit=_audit())


def test_store_mapping_audit_validation_and_record_limits(tmp_path: Path) -> None:
    valid_current = _snapshot().to_mapping()
    with pytest.raises(ValueError, match="current"):
        _state_from_mapping({"current": None})
    with pytest.raises(ValueError, match="previous token"):
        _state_from_mapping({"current": valid_current, "previous": []})
    with pytest.raises(ValueError, match="previous token expiry"):
        _state_from_mapping({"current": valid_current, "previous_expires_at": 7})

    store = SQLiteControlPlaneStore(tmp_path / "audit.sqlite3", audit_key=AUDIT_KEY)
    for limit in (0, 10_001):
        with pytest.raises(ValueError, match="limit"):
            store.audit_records(limit=limit)

    with pytest.raises(ValueError, match="timezone-aware"):
        store.append_audit(_audit(timestamp=datetime(2026, 7, 29)))
    with pytest.raises(ValueError, match="correlation_id"):
        store.append_audit(_audit(correlation_id=""))
    with pytest.raises(ValueError, match="control"):
        store.append_audit(_audit(action="bad\nvalue"))
    with pytest.raises(ValueError, match="1024"):
        store.append_audit(_audit(details={"reason": "x" * 1025}))
    with pytest.raises(TypeError, match="scalar"):
        store.append_audit(_audit(details={"nested": {"unsafe": True}}))

    record = store.append_audit(_audit(details={"codes": [1, "two", None]}))
    assert record.details == {"codes": [1, "two", None]}


def test_store_detects_structural_state_and_audit_corruption(tmp_path: Path) -> None:
    state = _state(1)

    non_object_path = tmp_path / "non-object.sqlite3"
    non_object = SQLiteControlPlaneStore(non_object_path, audit_key=AUDIT_KEY)
    non_object.bootstrap(state, audit=_audit())
    with closing(sqlite3.connect(non_object_path)) as connection:
        connection.execute(
            "UPDATE control_plane_state SET state_json = ?, state_fingerprint = ? WHERE singleton = 1",
            ("[]", _fingerprint("[]")),
        )
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="JSON object"):
        non_object.load_state()

    generation_path = tmp_path / "generation.sqlite3"
    generation_store = SQLiteControlPlaneStore(generation_path, audit_key=AUDIT_KEY)
    generation_store.bootstrap(state, audit=_audit())
    state_json = json.dumps(
        {
            "current": _snapshot(2).to_mapping(),
            "previous": None,
            "previous_expires_at": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with closing(sqlite3.connect(generation_path)) as connection:
        connection.execute(
            "UPDATE control_plane_state SET state_json = ?, state_fingerprint = ? WHERE singleton = 1",
            (state_json, _fingerprint(state_json)),
        )
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="generation"):
        generation_store.load_state()

    chain_path = tmp_path / "chain.sqlite3"
    chain_store = SQLiteControlPlaneStore(chain_path, audit_key=AUDIT_KEY)
    chain_store.bootstrap(state, audit=_audit())
    chain_store.append_audit(_audit(action="security.second"))
    with closing(sqlite3.connect(chain_path)) as connection:
        connection.execute(
            "UPDATE control_plane_audit SET previous_mac = ? WHERE sequence = 2",
            ("f" * 64,),
        )
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="previous MAC"):
        chain_store.verify_audit_chain()


def test_store_audit_record_decoder_rejects_non_object_details(tmp_path: Path) -> None:
    path = tmp_path / "record.sqlite3"
    store = SQLiteControlPlaneStore(path, audit_key=AUDIT_KEY)
    store.append_audit(_audit())
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE control_plane_audit SET details_json = '[]' WHERE sequence = 1")
        connection.commit()
    with pytest.raises(AuditIntegrityError, match="JSON object"):
        store.audit_records()


def test_state_row_loader_rejects_unexpected_row_type_after_typing_repair() -> None:
    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("unexpected",)

    class Connection:
        def execute(self, _statement: str) -> Cursor:
            return Cursor()

    with pytest.raises(AuditIntegrityError, match="did not return a Row"):
        SQLiteControlPlaneStore._load_state_row(Connection())  # type: ignore[arg-type]
