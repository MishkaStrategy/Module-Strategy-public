from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .contracts import Component, ObservabilityPolicy, OperationalOverview, SourceRead, utc
from .projector import build_overview
from .sources import ObservationSource


class ObservabilityService:
    """Read-only composition over authoritative owner query surfaces."""

    def __init__(
        self,
        *,
        policy: ObservabilityPolicy,
        sources: Sequence[ObservationSource],
    ) -> None:
        if not isinstance(policy, ObservabilityPolicy):
            raise TypeError("policy must be ObservabilityPolicy")
        source_map: dict[Component, ObservationSource] = {}
        for source in sources:
            component = source.component
            if not isinstance(component, Component):
                raise TypeError("observation source component must be Component")
            if component in source_map:
                raise ValueError(f"duplicate observation source: {component.value}")
            source_map[component] = source
        self.policy = policy
        self._sources = source_map

    @property
    def configured_components(self) -> tuple[Component, ...]:
        return tuple(sorted(self._sources, key=lambda component: component.value))

    def snapshot(self, *, now: datetime) -> OperationalOverview:
        moment = utc(now, "now")
        reads: dict[Component, SourceRead] = {}
        for component in self.policy.required_components:
            source = self._sources.get(component)
            if source is None:
                reads[component] = SourceRead(
                    component=component,
                    observation=None,
                    unavailable_reason="owner_unavailable",
                )
                continue
            try:
                read = source.read(now=moment)
            except Exception:
                reads[component] = SourceRead(
                    component=component,
                    observation=None,
                    unavailable_reason="owner_read_failed",
                )
                continue
            if not isinstance(read, SourceRead) or read.component is not component:
                reads[component] = SourceRead(
                    component=component,
                    observation=None,
                    unavailable_reason="owner_state_invalid",
                )
                continue
            reads[component] = read
        return build_overview(reads, now=moment, policy=self.policy)
