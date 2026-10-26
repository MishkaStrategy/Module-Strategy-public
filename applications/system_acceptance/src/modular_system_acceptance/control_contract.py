"""Canonical owner surface approved for Control Plane integration."""

from .composition import build_complete_acceptance_registry, required_non_soak_phases
from .contracts import (
    AcceptanceCheckpoint,
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceMutationResult,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSnapshot,
    AcceptanceRunSpec,
    AcceptanceStatus,
    EvidenceReference,
    PhaseOutcome,
    PhaseOutcomeStatus,
    ResourceSample,
    TrustedOperator,
)
from .daemon import SystemAcceptanceDaemon
from .executors import CheckpointedSoakExecutor
from .native_executor import NativeOwnerPhaseExecutor
from .owner import (
    AcceptanceExecutorRegistry,
    AcceptancePhaseExecutor,
    PhaseExecutionContext,
    SystemAcceptanceOwner,
)
from .query import SystemAcceptanceQuery
from .service import SystemAcceptanceService
from .storage import SQLiteAcceptanceStore
from .trusted_http import PinnedSystemAcceptanceHttpServer

__all__ = [
    "AcceptanceCheckpoint",
    "AcceptanceErrorCode",
    "AcceptanceExecutorRegistry",
    "AcceptanceMutation",
    "AcceptanceMutationRequest",
    "AcceptanceMutationResult",
    "AcceptanceOwnerError",
    "AcceptancePhase",
    "AcceptancePhaseExecutor",
    "AcceptanceRunSnapshot",
    "AcceptanceRunSpec",
    "AcceptanceStatus",
    "CheckpointedSoakExecutor",
    "EvidenceReference",
    "NativeOwnerPhaseExecutor",
    "PhaseExecutionContext",
    "PhaseOutcome",
    "PhaseOutcomeStatus",
    "PinnedSystemAcceptanceHttpServer",
    "ResourceSample",
    "SQLiteAcceptanceStore",
    "SystemAcceptanceDaemon",
    "SystemAcceptanceOwner",
    "SystemAcceptanceQuery",
    "SystemAcceptanceService",
    "TrustedOperator",
    "build_complete_acceptance_registry",
    "required_non_soak_phases",
]
