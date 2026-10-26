from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceDaemon,
    SystemAcceptanceOwner,
)
from modular_system_acceptance.service import SystemAcceptanceService
from modular_system_acceptance.trusted_http import PinnedSystemAcceptanceHttpServer

NOW = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


class Executor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


class FailingStartHttp(PinnedSystemAcceptanceHttpServer):
    def start(self) -> None:
        raise RuntimeError("simulated bind failure")


def build_owner(
    path: Path,
) -> tuple[SystemAcceptanceOwner, SystemAcceptanceDaemon, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="service-owner",
        clock=lambda: NOW,
        lease_seconds=5,
    )
    return owner, SystemAcceptanceDaemon(owner, poll_seconds=0.05), store


def build_service(path: Path) -> tuple[SystemAcceptanceService, SQLiteAcceptanceStore]:
    owner, daemon, store = build_owner(path)
    http = PinnedSystemAcceptanceHttpServer(
        owner,
        full_rc_sha="a" * 40,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
    )
    return SystemAcceptanceService(daemon, http), store


def test_service_starts_once_and_closes_cleanly(tmp_path: Path) -> None:
    service, store = build_service(tmp_path / "acceptance.db")
    try:
        service.start()
        service.start()
        deadline = time.monotonic() + 2
        while not service.running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.running is True
    finally:
        service.close()
        store.close()
    assert service.running is False


def test_service_close_before_start_is_safe_and_terminal(tmp_path: Path) -> None:
    service, store = build_service(tmp_path / "acceptance.db")
    service.close()
    try:
        assert service.running is False
        with pytest.raises(RuntimeError, match="cannot restart after close"):
            service.start()
        service.close()
    finally:
        store.close()


def test_failed_http_start_closes_service_and_forbids_retry(tmp_path: Path) -> None:
    owner, daemon, store = build_owner(tmp_path / "acceptance.db")
    http = FailingStartHttp(
        owner,
        full_rc_sha="a" * 40,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
    )
    service = SystemAcceptanceService(daemon, http)
    try:
        with pytest.raises(RuntimeError, match="simulated bind failure"):
            service.start()
        assert service.running is False
        assert daemon.stopped is True
        with pytest.raises(RuntimeError, match="cannot restart after close"):
            service.start()
    finally:
        service.close()
        store.close()
