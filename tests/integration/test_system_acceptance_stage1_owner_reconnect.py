from __future__ import annotations

from dataclasses import dataclass

import pytest

from modular_research_runner.owner_http import ResearchRunnerOwnerHttpClient
from modular_research_runner.owner_identity import (
    PreparedResearchIdentity,
    ResearchExperimentOwnerIdentity,
)
from modular_system_acceptance import AcceptanceErrorCode, AcceptanceOwnerError
from modular_system_acceptance.production_research import ResearchRunnerPort
from modular_trading_runner.acceptance_http import TradingRunnerAcceptanceHttpQuery
from scripts.system_acceptance_stage1_deployment import (
    _require_runtime_configuration_identity,
    _runtime_configuration_identity,
    _runtime_snapshot,
    _trading_runtime_query,
)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    runtime_id: str = "runtime-stage1"
    portfolio_id: str = "portfolio-paper"
    strategy_id: str = "strategy-btc-paper"
    execution_mode: str = "paper"
    base_timeframe: str = "5m"
    configuration_hash: str = "a" * 64
    native_state: str = "RUNNING"
    ready: bool = True
    live: bool = True
    component_count: int = 3
    critical_failure_count: int = 0


class _Query:
    def __init__(self, snapshot: object | None = None, *, error: Exception | None = None) -> None:
        self._snapshot = snapshot if snapshot is not None else _Snapshot()
        self._error = error
        self.calls = 0

    def snapshot(self) -> object:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


def _research_port(value: ResearchRunnerPort) -> ResearchRunnerPort:
    return value


def test_owner_local_http_clients_match_stage1_structural_ports_without_io() -> None:
    research = ResearchRunnerOwnerHttpClient(
        "http://127.0.0.1:8782",
        experiment_id="exp-" + "b" * 24,
        reader_token="reader-token-0123456789",
        operator_token="operator-token-0123456789",
    )
    trading = TradingRunnerAcceptanceHttpQuery(
        "http://127.0.0.1:8780",
        reader_token="reader-token-0123456789",
    )

    assert _research_port(research) is research
    assert _trading_runtime_query(trading) is trading


def test_protocol_query_yields_frozen_runtime_configuration_identity() -> None:
    query = _Query()

    normalized = _trading_runtime_query(query)  # type: ignore[arg-type]
    snapshot = _runtime_snapshot(normalized)
    identity = _runtime_configuration_identity(snapshot)

    assert normalized is query
    assert query.calls == 1
    assert identity.runtime_id == "runtime-stage1"
    assert identity.portfolio_id == "portfolio-paper"
    assert identity.strategy_id == "strategy-btc-paper"
    assert identity.execution_mode == "paper"
    assert identity.config_digest == "a" * 64


def test_research_owner_identity_adapts_without_preparing_experiment() -> None:
    owner = ResearchExperimentOwnerIdentity(
        experiment_id="exp-" + "b" * 24,
        specification_sha256="c" * 64,
        strategy_id="strategy-btc-paper",
    )

    prepared = PreparedResearchIdentity.from_owner_identity(owner)

    assert prepared.manifest.experiment_id == owner.experiment_id
    assert prepared.specification.fingerprint == owner.specification_sha256
    assert prepared.specification.strategy_id == owner.strategy_id
    assert not hasattr(prepared, "plan")
    assert not hasattr(prepared, "prepare")


@pytest.mark.parametrize(
    "drifted",
    [
        _Snapshot(configuration_hash="b" * 64),
        _Snapshot(runtime_id="runtime-other"),
        _Snapshot(portfolio_id="portfolio-other"),
        _Snapshot(strategy_id="strategy-other"),
    ],
)
def test_runtime_identity_drift_fails_closed_before_smoke_proof(drifted: _Snapshot) -> None:
    expected = _runtime_configuration_identity(_Snapshot())

    with pytest.raises(AcceptanceOwnerError, match="runtime identity changed") as captured:
        _require_runtime_configuration_identity(drifted, expected)

    assert captured.value.code is AcceptanceErrorCode.PHASE_BLOCKED


@pytest.mark.parametrize(
    ("snapshot", "message"),
    [
        (_Snapshot(configuration_hash="not-a-digest"), "configuration_hash"),
        (_Snapshot(component_count=-1), "component_count"),
        (_Snapshot(component_count=0, critical_failure_count=1), "health counts"),
    ],
)
def test_runtime_snapshot_rejects_malformed_owner_projection(
    snapshot: _Snapshot,
    message: str,
) -> None:
    with pytest.raises(AcceptanceOwnerError, match=message) as captured:
        _runtime_snapshot(_trading_runtime_query(_Query(snapshot)))  # type: ignore[arg-type]

    assert captured.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_runtime_snapshot_rejects_non_paper_projection() -> None:
    with pytest.raises(AcceptanceOwnerError, match="PAPER-only") as captured:
        _runtime_snapshot(
            _trading_runtime_query(_Query(_Snapshot(execution_mode="live")))  # type: ignore[arg-type]
        )

    assert captured.value.code is AcceptanceErrorCode.PAPER_ONLY


def test_runtime_query_failure_is_bounded_recovery_error() -> None:
    query = _Query(error=RuntimeError("private runtime failure with local details"))

    with pytest.raises(AcceptanceOwnerError, match="snapshot is unavailable") as captured:
        _runtime_snapshot(_trading_runtime_query(query))  # type: ignore[arg-type]

    assert captured.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED
    assert "private runtime failure" not in str(captured.value)
