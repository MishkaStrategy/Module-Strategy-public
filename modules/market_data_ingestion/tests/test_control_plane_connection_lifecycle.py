from __future__ import annotations

import gc
import os
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_data_application import (
    ApplicationSettings,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.control_plane_http import ControlPlaneSecurityController
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import (
    AuditContext,
    ControlPlaneStateError,
    SQLiteControlPlaneStore,
)
from market_data_application.security import ApiRole
from market_data_application.server import create_server
from market_data_ingestion.adapters import FakeExchangeAdapter

_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
_ADMIN_TOKEN = "lifecycle-admin-token-000000000000000000000000"
_AUDIT_KEY = b"lifecycle-audit-key-for-tests-at-least-32-bytes"


def _state() -> TokenSnapshotState:
    record = ApiTokenRecord.from_plaintext(
        token_id="lifecycle-admin",
        token=_ADMIN_TOKEN,
        role=ApiRole.ADMIN,
        not_before=_NOW,
    )
    return TokenSnapshotState(
        current=ApiTokenSnapshot(generation=1, created_at=_NOW, records=(record,))
    )


def _audit(action: str) -> AuditContext:
    return AuditContext(
        timestamp=_NOW,
        correlation_id="corr-lifecycle",
        direct_peer="127.0.0.1",
        principal_id="lifecycle-admin",
        token_id="lifecycle-admin",
        action=action,
        outcome="accepted",
    )


def _resource_warnings(captured: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [item for item in captured if issubclass(item.category, ResourceWarning)]


def _fd_count() -> int | None:
    fd_root = Path("/dev/fd")
    if not fd_root.is_dir():
        return None
    return len(os.listdir(fd_root))


def test_control_plane_store_closes_connections_on_success_and_failure(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        for index in range(10):
            store = SQLiteControlPlaneStore(
                tmp_path / f"control-plane-{index}.sqlite3",
                audit_key=_AUDIT_KEY,
            )
            state = _state()
            assert store.bootstrap(state, audit=_audit("security.bootstrap")) == state
            assert store.load_state() == state
            store.append_audit(_audit("security.read"))
            assert store.verify_audit_chain().record_count == 2
            assert len(store.audit_records()) == 2
            with pytest.raises(ControlPlaneStateError, match="changed"):
                store.replace_state(
                    state,
                    expected_generation=99,
                    audit=_audit("security.reload"),
                )
        del store
        gc.collect()

    assert _resource_warnings(captured) == []


def test_repeated_hardened_api_requests_keep_fd_and_warning_counts_stable(
    tmp_path: Path,
) -> None:
    state = _state()
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3", audit_key=_AUDIT_KEY)
    store.bootstrap(state, audit=_audit("security.bootstrap"))
    controller = ControlPlaneSecurityController(
        authenticator=ReloadableApiTokenAuthenticator(state, clock=lambda: _NOW),
        store=store,
        limits=ControlPlaneLimits(),
        peer_limiter=BoundedTokenBucketLimiter(
            capacity=1_000,
            refill_per_second=1_000.0,
            max_entries=16,
            clock=lambda: 0.0,
        ),
        principal_limiter=BoundedTokenBucketLimiter(
            capacity=1_000,
            refill_per_second=1_000.0,
            max_entries=16,
            clock=lambda: 0.0,
        ),
        clock=lambda: _NOW,
    )
    application = MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path / "data",
                instance_id="control-plane-lifecycle-test",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )
    before_fds = _fd_count()

    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            with TestClient(create_server(application, control_plane=controller)) as client:
                for _ in range(50):
                    assert client.get("/api/v1/jobs").status_code == 401
                    assert (
                        client.get(
                            "/api/v1/jobs",
                            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                        ).status_code
                        == 200
                    )
                    assert (
                        client.get(
                            "/api/v1/jobs?limit=0",
                            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
                        ).status_code
                        == 422
                    )
            gc.collect()

        assert _resource_warnings(captured) == []
    finally:
        application.shutdown()

    gc.collect()
    after_fds = _fd_count()
    if before_fds is not None and after_fds is not None:
        assert after_fds <= before_fds + 4
