from __future__ import annotations

from pydantic import ValidationError

from .client import ClientSettings, ControlClient, ControlClientError, Transport
from .observability import UnifiedObservabilitySnapshot


class ObservabilityControlClient(ControlClient):
    """Read-only client for the versioned unified observability projection."""

    def __init__(
        self,
        settings: ClientSettings | None = None,
        *,
        transport: Transport | None = None,
    ) -> None:
        super().__init__(settings, transport=transport)

    def observability(self) -> UnifiedObservabilitySnapshot:
        payload = self._get("/api/control/v1/observability/overview")
        try:
            return UnifiedObservabilitySnapshot.model_validate(payload)
        except ValidationError as exc:
            raise ControlClientError(
                "CONTROL_RESPONSE_INVALID",
                "Control Plane returned an incompatible unified observability response.",
                action="Verify that modular-control and modular-control-api use compatible versions.",
            ) from exc


__all__ = ["ObservabilityControlClient"]
