from __future__ import annotations

from pydantic import ValidationError

from .client import ControlClient, ControlClientError
from .paper_lifecycle import (
    PaperLifecycleMutation,
    PaperLifecycleMutationRequest,
    PaperLifecycleMutationResult,
)

_PATHS = {
    PaperLifecycleMutation.START: "/api/control/v1/paper/start",
    PaperLifecycleMutation.STOP: "/api/control/v1/paper/stop",
    PaperLifecycleMutation.ACTIVATE_KILL_SWITCH: "/api/control/v1/paper/kill-switch/activate",
    PaperLifecycleMutation.RESET_KILL_SWITCH: "/api/control/v1/paper/kill-switch/reset",
}


class PaperLifecycleControlClient(ControlClient):
    """ControlClient extension for owner-native PAPER lifecycle mutations."""

    def paper_lifecycle_mutate(
        self,
        operation: PaperLifecycleMutation,
        request: PaperLifecycleMutationRequest,
    ) -> PaperLifecycleMutationResult:
        if not isinstance(operation, PaperLifecycleMutation):
            raise TypeError("operation must be PaperLifecycleMutation")
        if not isinstance(request, PaperLifecycleMutationRequest):
            raise TypeError("request must be PaperLifecycleMutationRequest")
        payload = self._post(_PATHS[operation], request.model_dump(mode="json"))
        try:
            result = PaperLifecycleMutationResult.model_validate(payload)
        except ValidationError as exc:
            raise _invalid_paper_lifecycle_response() from exc
        if result.request_id != request.request_id or result.mutation is not operation:
            raise _invalid_paper_lifecycle_response()
        return result


def _invalid_paper_lifecycle_response() -> ControlClientError:
    return ControlClientError(
        "CONTROL_RESPONSE_INVALID",
        "Control Plane returned an incompatible PAPER lifecycle mutation response.",
        action="Verify that modular-control and modular-control-api use compatible versions.",
    )


__all__ = ["PaperLifecycleControlClient"]
