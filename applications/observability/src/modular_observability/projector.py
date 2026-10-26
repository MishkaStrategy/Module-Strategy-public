from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .contracts import (
    Component,
    ComponentHealth,
    ComponentObservation,
    ObservabilityPolicy,
    OperationalOverview,
    SourceRead,
    utc,
)


def _ages(
    *,
    now: datetime,
    observed_at: datetime,
    progress_at: datetime | None,
    maximum_future_skew_seconds: float,
) -> tuple[float, float | None, str | None]:
    observed_delta = (now - observed_at).total_seconds()
    if observed_delta < -maximum_future_skew_seconds:
        return 0.0, None, "source_clock_anomaly"
    progress_age: float | None = None
    if progress_at is not None:
        progress_delta = (now - progress_at).total_seconds()
        if progress_delta < -maximum_future_skew_seconds:
            return max(0.0, observed_delta), 0.0, "source_progress_clock_anomaly"
        progress_age = max(0.0, progress_delta)
    return max(0.0, observed_delta), progress_age, None


def project_component(
    read: SourceRead,
    *,
    now: datetime,
    policy: ObservabilityPolicy,
) -> ComponentObservation:
    """Project one owner read without mutating or reconstructing owner truth."""

    moment = utc(now, "now")
    if read.component not in policy.freshness_seconds:
        raise ValueError(f"missing freshness policy for {read.component.value}")
    source = read.observation
    if source is None:
        assert read.unavailable_reason is not None
        return ComponentObservation(
            component=read.component,
            health=ComponentHealth.UNAVAILABLE,
            observed_at=None,
            evaluated_at=moment,
            age_seconds=None,
            stale=False,
            progress_at=None,
            progress_age_seconds=None,
            stuck=False,
            state_ref=None,
            reason_codes=(read.unavailable_reason,),
        )

    age_seconds, progress_age_seconds, clock_reason = _ages(
        now=moment,
        observed_at=source.observed_at,
        progress_at=source.progress_at,
        maximum_future_skew_seconds=policy.maximum_future_skew_seconds,
    )
    if clock_reason is not None:
        reasons = tuple(sorted(set(source.reason_codes) | {clock_reason}))
        return ComponentObservation(
            component=read.component,
            health=ComponentHealth.UNAVAILABLE,
            observed_at=source.observed_at,
            evaluated_at=moment,
            age_seconds=age_seconds,
            stale=False,
            progress_at=source.progress_at,
            progress_age_seconds=progress_age_seconds,
            stuck=False,
            state_ref=source.state_ref,
            reason_codes=reasons,
            correlation_id=source.correlation_id,
            public_details=source.public_details,
        )

    threshold = policy.freshness_seconds[read.component]
    if age_seconds > threshold:
        reasons = tuple(sorted(set(source.reason_codes) | {"source_stale"}))
        return ComponentObservation(
            component=read.component,
            health=ComponentHealth.UNAVAILABLE,
            observed_at=source.observed_at,
            evaluated_at=moment,
            age_seconds=age_seconds,
            stale=True,
            progress_at=source.progress_at,
            progress_age_seconds=progress_age_seconds,
            stuck=False,
            state_ref=source.state_ref,
            reason_codes=reasons,
            correlation_id=source.correlation_id,
            public_details=source.public_details,
        )

    stuck = False
    health = source.health
    reasons = source.reason_codes
    stuck_threshold = policy.stuck_seconds.get(read.component)
    if (
        stuck_threshold is not None
        and progress_age_seconds is not None
        and progress_age_seconds > stuck_threshold
    ):
        stuck = True
        reasons = tuple(sorted(set(reasons) | {"source_progress_stalled"}))
        if health is ComponentHealth.HEALTHY:
            health = ComponentHealth.DEGRADED

    return ComponentObservation(
        component=read.component,
        health=health,
        observed_at=source.observed_at,
        evaluated_at=moment,
        age_seconds=age_seconds,
        stale=False,
        progress_at=source.progress_at,
        progress_age_seconds=progress_age_seconds,
        stuck=stuck,
        state_ref=source.state_ref,
        reason_codes=reasons,
        correlation_id=source.correlation_id,
        public_details=source.public_details,
    )


def _overall_health(components: tuple[ComponentObservation, ...]) -> ComponentHealth:
    healths = {item.health for item in components}
    if ComponentHealth.UNAVAILABLE in healths:
        return ComponentHealth.UNAVAILABLE
    if ComponentHealth.BLOCKED in healths:
        return ComponentHealth.BLOCKED
    if ComponentHealth.DEGRADED in healths:
        return ComponentHealth.DEGRADED
    return ComponentHealth.HEALTHY


def build_overview(
    reads: Mapping[Component, SourceRead],
    *,
    now: datetime,
    policy: ObservabilityPolicy,
) -> OperationalOverview:
    """Build one deterministic fail-closed view over all required owner components."""

    moment = utc(now, "now")
    invalid_keys = [key for key in reads if not isinstance(key, Component)]
    if invalid_keys:
        raise TypeError("reads keys must be Component values")
    projected: list[ComponentObservation] = []
    for component in sorted(policy.required_components, key=lambda item: item.value):
        read = reads.get(component)
        if read is None:
            read = SourceRead(
                component=component,
                observation=None,
                unavailable_reason="owner_unavailable",
            )
        elif read.component is not component:
            raise ValueError(f"read key/component mismatch for {component.value}")
        projected.append(project_component(read, now=moment, policy=policy))
    components = tuple(projected)
    return OperationalOverview(
        evaluated_at=moment,
        overall_health=_overall_health(components),
        components=components,
    )
