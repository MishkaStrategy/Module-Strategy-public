from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from .adapters.trading_runner_lifecycle_http import (
    TradingRunnerLifecycleHttpAdapter,
    TradingRunnerLifecycleOperation,
)
from .contracts import ContractModel

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

RuntimeState = Literal[
    "created",
    "configuring",
    "configured",
    "initializing",
    "recovering",
    "reconciling",
    "starting",
    "running",
    "degraded",
    "pausing",
    "paused",
    "stopping",
    "stopped",
    "failed",
    "kill_switch_active",
    "disposed",
]


class PaperLifecycleMutation(StrEnum):
    START = "START"
    STOP = "STOP"
    ACTIVATE_KILL_SWITCH = "ACTIVATE_KILL_SWITCH"
    RESET_KILL_SWITCH = "RESET_KILL_SWITCH"


class PaperLifecycleCapability(ContractModel):
    available: bool
    paper_only: Literal[True] = True


class PaperLifecycleMutationRequest(ContractModel):
    request_id: str = Field(min_length=1, max_length=128)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("request_id must be a bounded portable identifier")
        return value


class PaperLifecycleMutationResult(ContractModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    operator: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$",
    )
    mutation: PaperLifecycleMutation
    previous_state: RuntimeState
    new_state: RuntimeState
    config_hash: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    completed_at: datetime
    replayed: bool

    @field_validator("completed_at")
    @classmethod
    def require_aware_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value.astimezone(UTC)


class PaperLifecycleControlService:
    def __init__(self, adapter: TradingRunnerLifecycleHttpAdapter) -> None:
        if not isinstance(adapter, TradingRunnerLifecycleHttpAdapter):
            raise TypeError("adapter must be TradingRunnerLifecycleHttpAdapter")
        self._adapter = adapter

    def start(self, request: PaperLifecycleMutationRequest) -> PaperLifecycleMutationResult:
        return self._mutate(PaperLifecycleMutation.START, request)

    def stop(self, request: PaperLifecycleMutationRequest) -> PaperLifecycleMutationResult:
        return self._mutate(PaperLifecycleMutation.STOP, request)

    def activate_kill_switch(
        self,
        request: PaperLifecycleMutationRequest,
    ) -> PaperLifecycleMutationResult:
        return self._mutate(PaperLifecycleMutation.ACTIVATE_KILL_SWITCH, request)

    def reset_kill_switch(
        self,
        request: PaperLifecycleMutationRequest,
    ) -> PaperLifecycleMutationResult:
        return self._mutate(PaperLifecycleMutation.RESET_KILL_SWITCH, request)

    def _mutate(
        self,
        mutation: PaperLifecycleMutation,
        request: PaperLifecycleMutationRequest,
    ) -> PaperLifecycleMutationResult:
        if not isinstance(request, PaperLifecycleMutationRequest):
            raise TypeError("request must be PaperLifecycleMutationRequest")
        owner_operation = TradingRunnerLifecycleOperation(mutation.value)
        result = self._adapter.mutate(owner_operation, request.request_id)
        return PaperLifecycleMutationResult(
            request_id=result.request_id,
            operator=result.operator,
            mutation=mutation,
            previous_state=result.previous_state,
            new_state=result.new_state,
            config_hash=result.config_hash,
            completed_at=result.completed_at,
            replayed=result.replayed,
        )


__all__ = [
    "PaperLifecycleCapability",
    "PaperLifecycleControlService",
    "PaperLifecycleMutation",
    "PaperLifecycleMutationRequest",
    "PaperLifecycleMutationResult",
]
