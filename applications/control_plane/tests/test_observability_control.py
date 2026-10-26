from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from modular_control_plane.contracts import ComponentHealth, ComponentId, HealthStatus, SafeScalar
from modular_control_plane.observability import (
    ObservabilityComponentId,
    ObservabilityComponentView,
    ObservabilityHealth,
    UnifiedOperationalOverview,
)
from modular_control_plane.observability_service import build_observability_control
from modular_control_plane.service import ControlPlaneService
from modular_control_plane.system_resources import (
    ResourceUsage,
    SystemResourcesControlService,
    SystemResourcesSnapshot,
)

NOW = datetime(2026, 8, 28, 4, 40, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


@dataclass(frozen=True)
class StaticAdapter:
    component: ComponentId
    status: HealthStatus = HealthStatus.HEALTHY
    details: dict[str, SafeScalar] = field(default_factory=dict)
    summary: str = "owner projection"

    def health(self) -> ComponentHealth:
        return ComponentHealth(
            component=self.component,
            status=self.status,
            checked_at=NOW,
            summary=self.summary,
            details=self.details,
        )


class StaticResources:
    def snapshot(self) -> SystemResourcesSnapshot:
        return SystemResourcesSnapshot(
            collected_at=NOW,
            cpu_percent=12.5,
            logical_cpu_count=8,
            memory=ResourceUsage(
                total_bytes=1_000_000,
                used_bytes=400_000,
                available_bytes=600_000,
                percent=40.0,
            ),
            disk=ResourceUsage(
                total_bytes=2_000_000,
                used_bytes=500_000,
                available_bytes=1_500_000,
                percent=25.0,
            ),
            uptime_seconds=600.0,
            process_count=40,
            control_process_rss_bytes=12_000_000,
            control_process_threads=4,
        )


def _service(*, overrides: dict[ComponentId, StaticAdapter] | None = None) -> ControlPlaneService:
    current = dict(overrides or {})
    adapters = []
    for component in ComponentId:
        if component in current:
            adapters.append(current[component])
            continue
        details: dict[str, SafeScalar] = {}
        if component is ComponentId.MARKET_DATA:
            details = {"registry": "ok"}
        adapters.append(StaticAdapter(component=component, details=details))
    return ControlPlaneService(adapters, required_components=tuple(ComponentId), clock=fixed_clock)


def _resources() -> SystemResourcesControlService:
    return SystemResourcesControlService(StaticResources())


def _component(snapshot, component: ObservabilityComponentId):
    return next(item for item in snapshot.overview.components if item.component is component)


def test_bridge_is_fail_closed_for_incomplete_or_unattached_owner_surfaces() -> None:
    control = build_observability_control(_service(), _resources(), clock=fixed_clock)

    snapshot = control.snapshot()

    assert snapshot.mode == "PAPER"
    assert len(snapshot.overview.components) == len(ObservabilityComponentId)
    assert snapshot.overview.overall_health is ObservabilityHealth.UNAVAILABLE
    for component in (
        ObservabilityComponentId.MARKET_DATA,
        ObservabilityComponentId.BACKTEST_RESEARCH,
        ObservabilityComponentId.STRATEGY_AUDIT_FACTORY,
        ObservabilityComponentId.RISK_SUPERVISOR,
        ObservabilityComponentId.RISK_SIZING,
        ObservabilityComponentId.PORTFOLIO_POSITION,
        ObservabilityComponentId.TRADING_RUNNER,
        ObservabilityComponentId.PAPER_EXECUTION,
        ObservabilityComponentId.SYSTEM_ACCEPTANCE_RECOVERY,
    ):
        assert _component(snapshot, component).health is ObservabilityHealth.UNAVAILABLE
    assert (
        _component(snapshot, ObservabilityComponentId.DATASET_REGISTRY).health
        is ObservabilityHealth.HEALTHY
    )
    assert (
        _component(snapshot, ObservabilityComponentId.SIGNAL_BOT).health
        is ObservabilityHealth.HEALTHY
    )
    assert (
        _component(snapshot, ObservabilityComponentId.SYSTEM_RESOURCES).health
        is ObservabilityHealth.HEALTHY
    )
    active_components = {alert.component for alert in snapshot.active_alerts}
    assert ObservabilityComponentId.MARKET_DATA in active_components
    assert ObservabilityComponentId.BACKTEST_RESEARCH in active_components
    assert ObservabilityComponentId.STRATEGY_AUDIT_FACTORY in active_components
    assert ObservabilityComponentId.RISK_SUPERVISOR in active_components
    assert ObservabilityComponentId.PORTFOLIO_POSITION in active_components
    assert ObservabilityComponentId.SYSTEM_ACCEPTANCE_RECOVERY in active_components


def test_readable_market_data_api_cannot_claim_market_freshness() -> None:
    market = StaticAdapter(
        component=ComponentId.MARKET_DATA,
        status=HealthStatus.HEALTHY,
        details={"registry": "ok", "jobs": "ok"},
        summary="Market Data API is reachable",
    )
    control = build_observability_control(
        _service(overrides={ComponentId.MARKET_DATA: market}),
        _resources(),
        clock=fixed_clock,
    )

    snapshot = control.snapshot()

    market_view = _component(snapshot, ObservabilityComponentId.MARKET_DATA)
    registry_view = _component(snapshot, ObservabilityComponentId.DATASET_REGISTRY)
    assert market_view.health is ObservabilityHealth.UNAVAILABLE
    assert market_view.reason_codes == ("owner_unavailable",)
    assert registry_view.health is ObservabilityHealth.HEALTHY
    assert registry_view.public_details["registry_status"] == "ok"


def test_bridge_does_not_publish_legacy_private_details_or_summary() -> None:
    private_canary = "/Users/operator/private/TOP-SECRET-token"
    signal = StaticAdapter(
        component=ComponentId.SIGNAL_BOT,
        status=HealthStatus.DEGRADED,
        details={"private_path": private_canary},
        summary=private_canary,
    )
    control = build_observability_control(
        _service(overrides={ComponentId.SIGNAL_BOT: signal}),
        _resources(),
        clock=fixed_clock,
    )

    snapshot = control.snapshot()
    payload = snapshot.model_dump_json()

    assert private_canary not in payload
    signal_view = _component(snapshot, ObservabilityComponentId.SIGNAL_BOT)
    assert signal_view.health is ObservabilityHealth.DEGRADED
    assert signal_view.public_details["control_status"] == "DEGRADED"


def test_native_risk_supervisor_source_preserves_backend_reason_and_alert_action() -> None:
    import modular_observability as backend

    risk_source = backend.CallableObservationSource(
        component=backend.Component.RISK_SUPERVISOR,
        query=lambda *, now: backend.SourceObservation(
            component=backend.Component.RISK_SUPERVISOR,
            health=backend.ComponentHealth.BLOCKED,
            observed_at=now,
            state_ref=backend.opaque_state_ref({"decision": "halt-7"}),
            reason_codes=("risk_supervisor.operator_emergency_halt",),
        ),
    )
    control = build_observability_control(
        _service(),
        _resources(),
        native_sources=(risk_source,),
        clock=fixed_clock,
    )

    snapshot = control.snapshot()
    risk = _component(snapshot, ObservabilityComponentId.RISK_SUPERVISOR)
    alert = next(
        item
        for item in snapshot.active_alerts
        if item.component is ObservabilityComponentId.RISK_SUPERVISOR
    )

    assert risk.health is ObservabilityHealth.BLOCKED
    assert risk.reason_codes == ("risk_supervisor.operator_emergency_halt",)
    assert alert.severity.value == "critical"
    assert alert.reason_codes == risk.reason_codes
    assert alert.next_action == "follow_risk_supervisor_recovery"
    assert alert.source_observation_hash == risk.observation_hash


def test_transport_rejects_green_overview_when_any_component_is_unavailable() -> None:
    snapshot = build_observability_control(_service(), _resources(), clock=fixed_clock).snapshot()

    with pytest.raises(ValidationError, match="overall_health must match component health"):
        UnifiedOperationalOverview(
            evaluated_at=snapshot.overview.evaluated_at,
            overall_health=ObservabilityHealth.HEALTHY,
            overview_hash=snapshot.overview.overview_hash,
            components=snapshot.overview.components,
        )


def test_transport_rejects_integer_that_web_cannot_represent_exactly() -> None:
    with pytest.raises(ValidationError, match="JSON safe-integer boundary"):
        ObservabilityComponentView(
            component=ObservabilityComponentId.SYSTEM_RESOURCES,
            health=ObservabilityHealth.HEALTHY,
            observed_at=NOW,
            evaluated_at=NOW,
            age_seconds=0.0,
            stale=False,
            progress_at=None,
            progress_age_seconds=None,
            stuck=False,
            state_ref="a" * 64,
            reason_codes=(),
            correlation_id=None,
            public_details={"unsafe_integer": 2**53},
            observation_hash="b" * 64,
        )
