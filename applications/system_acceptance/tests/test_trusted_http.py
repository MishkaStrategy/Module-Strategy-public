from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
)
from modular_system_acceptance.trusted_http import PinnedSystemAcceptanceHttpServer

NOW = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)
FULL_RC = "a" * 40
READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


class Executor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


def server(path: Path) -> tuple[PinnedSystemAcceptanceHttpServer, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="trusted-http-owner",
        clock=lambda: NOW,
        lease_seconds=5,
    )
    return (
        PinnedSystemAcceptanceHttpServer(
            owner,
            full_rc_sha=FULL_RC,
            reader_token=READER,
            operator_token=OPERATOR,
            port=0,
        ),
        store,
    )


def body(spec: dict[str, object]) -> bytes:
    return json.dumps({"request_id": "pinned-start", "spec": spec}).encode()


def valid_spec() -> dict[str, object]:
    return {
        "dataset_id": "dataset-v1",
        "dataset_digest": "b" * 64,
        "strategy_id": "strategy-v1",
        "strategy_digest": "c" * 64,
        "config_digest": "d" * 64,
        "enable_soak_24h": False,
        "enable_soak_72h": False,
    }


def decode(raw: bytes) -> dict[str, object]:
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def test_start_uses_trusted_full_rc_and_paper_mode(tmp_path: Path) -> None:
    current, store = server(tmp_path / "acceptance.db")
    try:
        code, _, response = current.response(
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=body(valid_spec()),
        )
        assert code == 200
        run_id = decode(response)["acceptance_run_id"]
        assert isinstance(run_id, str)
        snapshot = current._owner.snapshot(run_id)  # noqa: SLF001 - verifies owner truth
        assert snapshot is not None
        assert snapshot.spec.full_rc_sha == FULL_RC
        assert snapshot.spec.mode == "PAPER"
    finally:
        current.close()
        store.close()


def test_browser_cannot_supply_full_rc_or_live_mode(tmp_path: Path) -> None:
    for forbidden_key, value in (("full_rc_sha", "f" * 40), ("mode", "LIVE")):
        current, store = server(tmp_path / f"{forbidden_key}.db")
        spec = valid_spec()
        spec[forbidden_key] = value
        try:
            code, _, response = current.response(
                method="POST",
                path="/acceptance/v1/start",
                authorization=f"Bearer {OPERATOR}",
                body=body(spec),
            )
            assert code == 400
            assert decode(response)["error"] == "INVALID_REQUEST"
            assert current._owner.snapshot() is None  # noqa: SLF001
        finally:
            current.close()
            store.close()


def test_invalid_trusted_full_rc_rejected_before_server_creation(tmp_path: Path) -> None:
    store = SQLiteAcceptanceStore(tmp_path / "invalid.db")
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="invalid-trusted-owner",
        clock=lambda: NOW,
        lease_seconds=5,
    )
    try:
        try:
            PinnedSystemAcceptanceHttpServer(
                owner,
                full_rc_sha="not-a-git-sha",
                reader_token=READER,
                operator_token=OPERATOR,
                port=0,
            )
        except ValueError as exc:
            assert "full_rc_sha" in str(exc)
        else:
            raise AssertionError("invalid trusted full_rc_sha must be rejected")
    finally:
        store.close()
