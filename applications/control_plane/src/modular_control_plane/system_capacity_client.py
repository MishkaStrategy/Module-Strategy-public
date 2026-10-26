from __future__ import annotations

from pydantic import ValidationError

from .client import ControlClient, ControlClientError
from .system_artifact_capacity import ExternalArtifactCapacitySnapshot
from .system_capacity import SystemCapacitySnapshot


class SystemCapacityControlClient(ControlClient):
    """Typed reader client for the fail-closed System Capacity projection."""

    def system_capacity(self) -> SystemCapacitySnapshot:
        payload = self._get("/api/control/v1/system/capacity")
        try:
            return SystemCapacitySnapshot.model_validate(payload)
        except ValidationError as exc:
            raise ControlClientError(
                "CONTROL_RESPONSE_INVALID",
                "Control Plane returned an incompatible System Capacity response.",
                action="Verify that modular-control and modular-control-api use compatible versions.",
            ) from exc

    def external_artifact_capacity(self) -> ExternalArtifactCapacitySnapshot:
        payload = self._get("/api/control/v1/system/capacity/artifacts")
        try:
            return ExternalArtifactCapacitySnapshot.model_validate(payload)
        except ValidationError as exc:
            raise ControlClientError(
                "CONTROL_RESPONSE_INVALID",
                "Control Plane returned an incompatible external artifact capacity response.",
                action="Verify that modular-control and modular-control-api use compatible versions.",
            ) from exc


__all__ = ["SystemCapacityControlClient"]
