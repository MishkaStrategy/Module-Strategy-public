from __future__ import annotations

import pytest
from test_operator_http import OPERATOR, READER, build_server, decode

from modular_trading_runner import HealthStatus, RuntimeHealthRegistry, SupervisionPolicy
from modular_trading_runner.acceptance_http import TradingRunnerAcceptanceHttpQuery


def _install_authoritative_health(runner) -> None:  # type: ignore[no-untyped-def]
    health = RuntimeHealthRegistry(policy=SupervisionPolicy(), clock=runner.clock)
    health.register("runtime", critical=True)
    health.heartbeat("runtime", status=HealthStatus.HEALTHY)
    runner.health = health


def test_acceptance_http_query_reads_same_singleton_owner_without_mutation() -> None:
    server, runner, calls, _ = build_server()
    _install_authoritative_health(runner)
    server.start()
    host, port = server.address
    query = TradingRunnerAcceptanceHttpQuery(
        f"http://{host}:{port}",
        reader_token=READER,
    )
    try:
        snapshot = query.snapshot()
        assert snapshot.runtime_id == runner.config.runtime_id
        assert snapshot.portfolio_id == runner.config.portfolio_id
        assert snapshot.strategy_id == runner.config.strategy_id
        assert snapshot.execution_mode == "paper"
        assert snapshot.base_timeframe == runner.config.base_timeframe
        assert snapshot.configuration_hash == runner.config.canonical_hash
        assert snapshot.native_state == runner.state.value
        assert snapshot.ready is True
        assert snapshot.live is True
        assert snapshot.component_count == 1
        assert snapshot.critical_failure_count == 0
        assert calls == []
        assert READER not in repr(query)
    finally:
        server.close()


def test_acceptance_projection_is_lazy_and_fails_closed_without_authoritative_health() -> None:
    server, _, calls, _ = build_server()
    try:
        code, _, body = server.response(
            method="GET",
            path="/lifecycle/v1/acceptance",
            authorization=f"Bearer {READER}",
        )
        assert code == 503
        assert decode(body) == {"error": "OWNER_FAILURE"}
        assert calls == []
    finally:
        server.close()


def test_acceptance_http_query_rejects_remote_origins_and_operator_token() -> None:
    with pytest.raises(ValueError, match="loopback"):
        TradingRunnerAcceptanceHttpQuery(
            "http://example.com:8780",
            reader_token=READER,
        )

    server, _, _, _ = build_server()
    server.start()
    host, port = server.address
    query = TradingRunnerAcceptanceHttpQuery(
        f"http://{host}:{port}",
        reader_token=OPERATOR,
    )
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            query.snapshot()
    finally:
        server.close()
