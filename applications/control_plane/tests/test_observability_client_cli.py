from __future__ import annotations

import json
from datetime import UTC, datetime

from modular_control_plane.cli import main
from modular_control_plane.client import ClientSettings, ControlClientError
from modular_control_plane.observability_client import ObservabilityControlClient

NOW = datetime(2026, 8, 28, 4, 50, tzinfo=UTC)
DIGEST = "a" * 64


def _component(component: str, health: str, *, digest: str = DIGEST) -> dict[str, object]:
    reasons = [] if health == "healthy" else ["owner_unavailable"]
    return {
        "schema_version": 1,
        "component": component,
        "health": health,
        "observed_at": None if health == "unavailable" else NOW.isoformat(),
        "evaluated_at": NOW.isoformat(),
        "age_seconds": None if health == "unavailable" else 0.0,
        "stale": False,
        "progress_at": None,
        "progress_age_seconds": None,
        "stuck": False,
        "state_ref": None if health == "unavailable" else digest,
        "reason_codes": reasons,
        "correlation_id": None,
        "public_details": {},
        "observation_hash": digest,
    }


def _payload(*, overall: str = "unavailable") -> dict[str, object]:
    identifiers = [
        "backtest_research",
        "dataset_registry",
        "market_data",
        "paper_execution",
        "portfolio_position",
        "risk_sizing",
        "risk_supervisor",
        "signal_bot",
        "strategy_audit_factory",
        "system_acceptance_recovery",
        "system_resources",
        "trading_runner",
    ]
    health = {identifier: "healthy" for identifier in identifiers}
    if overall != "healthy":
        health["risk_supervisor"] = overall
    components = [
        _component(identifier, health[identifier], digest=f"{index + 1:064x}")
        for index, identifier in enumerate(identifiers)
    ]
    risk = next(item for item in components if item["component"] == "risk_supervisor")
    active_alerts: list[dict[str, object]] = []
    if overall != "healthy":
        active_alerts.append(
            {
                "schema_version": 1,
                "alert_id": "f" * 64,
                "component": "risk_supervisor",
                "severity": "critical",
                "reason_codes": ["owner_unavailable"],
                "next_action": "restore_owner_visibility",
                "evaluated_at": NOW.isoformat(),
                "source_observation_hash": risk["observation_hash"],
                "correlation_id": None,
            }
        )
    return {
        "schema_version": 1,
        "mode": "PAPER",
        "overview": {
            "schema_version": 1,
            "evaluated_at": NOW.isoformat(),
            "overall_health": overall,
            "overview_hash": "e" * 64,
            "components": components,
        },
        "active_alerts": active_alerts,
    }


def test_client_reads_strict_unified_observability_contract() -> None:
    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        assert url.endswith("/api/control/v1/observability/overview")
        assert headers["Authorization"] == "Bearer reader-secret"
        assert timeout == 1.5
        return 200, json.dumps(_payload()).encode()

    client = ObservabilityControlClient(
        ClientSettings(reader_token="reader-secret", timeout_seconds=1.5),
        transport=transport,
    )

    snapshot = client.observability()
    assert snapshot.overview.overall_health.value == "unavailable"
    assert snapshot.active_alerts[0].next_action == "restore_owner_visibility"


def test_client_rejects_incompatible_observability_response() -> None:
    client = ObservabilityControlClient(
        transport=lambda url, headers, timeout: (200, b'{"mode":"PAPER"}')
    )

    try:
        client.observability()
    except ControlClientError as exc:
        assert exc.code == "CONTROL_RESPONSE_INVALID"
        assert "observability" in exc.message
    else:
        raise AssertionError("invalid observability response must fail closed")


def test_system_observability_cli_uses_health_exit_codes_and_backend_next_action(
    monkeypatch,
    capsys,
) -> None:
    payload = _payload(overall="blocked")
    monkeypatch.setattr(
        ObservabilityControlClient,
        "observability",
        lambda self: _validate_payload(payload),
    )

    code = main(["system", "observability"])

    output = capsys.readouterr().out
    assert code == 3
    assert "BLOCKED" in output
    assert "risk_supervisor" in output
    assert "restore_owner_visibility" in output


def _validate_payload(payload: dict[str, object]):
    from modular_control_plane.observability import UnifiedObservabilitySnapshot

    return UnifiedObservabilitySnapshot.model_validate(payload)
