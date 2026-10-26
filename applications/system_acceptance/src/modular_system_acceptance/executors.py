from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from .contracts import (
    AcceptanceCheckpoint,
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseOutcome,
    PhaseOutcomeStatus,
    ResourceSample,
    SOAK_PHASES,
)
from .owner import AcceptancePhaseExecutor, PhaseExecutionContext

_DEFAULT_SOAK_TARGETS: Mapping[AcceptancePhase, float] = {
    AcceptancePhase.SOAK_2H: 2 * 60 * 60,
    AcceptancePhase.SOAK_24H: 24 * 60 * 60,
    AcceptancePhase.SOAK_72H: 72 * 60 * 60,
}
_SOAK_TOKEN = re.compile(r"soak:([A-Z0-9_]+):([0-9]{1,12}):([0-9]{1,16}):(healthy|paused)\Z")
_MAX_TARGET_SECONDS = 72 * 60 * 60
_MAX_SLICE_SECONDS = 300.0


class CheckpointedSoakExecutor(AcceptancePhaseExecutor):
    """Owner-native soak timer driven by persisted health heartbeats.

    Progress is elapsed time between consecutive healthy checkpoints, never a fixed
    amount per poll and never wall-clock time accumulated across recovery/downtime.
    A stale heartbeat gap contributes zero and establishes a fresh healthy baseline.
    """

    def __init__(
        self,
        healthy: Callable[[PhaseExecutionContext], bool],
        *,
        clock: Callable[[], datetime] | None = None,
        sample_resources: Callable[[], ResourceSample | None] | None = None,
        slice_seconds: float = 60.0,
        target_seconds: Mapping[AcceptancePhase, float] | None = None,
    ) -> None:
        if not callable(healthy):
            raise TypeError("healthy must be callable")
        if (
            isinstance(slice_seconds, bool)
            or not isinstance(slice_seconds, (int, float))
            or not 0.05 <= float(slice_seconds) <= _MAX_SLICE_SECONDS
        ):
            raise ValueError("slice_seconds must be between 0.05 and 300")
        targets = dict(_DEFAULT_SOAK_TARGETS if target_seconds is None else target_seconds)
        if set(targets) != set(SOAK_PHASES):
            raise ValueError("target_seconds must define every soak phase exactly once")
        normalized: dict[AcceptancePhase, float] = {}
        for phase, value in targets.items():
            if phase not in SOAK_PHASES:
                raise ValueError("target_seconds contains a non-soak phase")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.05 <= float(value) <= _MAX_TARGET_SECONDS
            ):
                raise ValueError("soak target must be between 0.05 seconds and 72 hours")
            normalized[phase] = float(value)
        self._healthy = healthy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sample_resources = sample_resources
        self._slice_seconds = float(slice_seconds)
        self._targets = normalized

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._slice(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._slice(context)

    def _slice(self, context: PhaseExecutionContext) -> PhaseOutcome:
        if context.phase not in SOAK_PHASES:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "checkpointed soak executor received a non-soak phase",
            )
        now = self._now()
        completed, previous_at, previous_healthy = _checkpoint_state(context)
        target = self._targets[context.phase]
        if completed + 1e-9 >= target:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.SUCCEEDED,
                active_seconds=0.0,
                checkpoint=_checkpoint(context, completed, now, healthy=True),
            )
        try:
            healthy = self._healthy(context)
        except Exception:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=0.0,
                checkpoint=_checkpoint(context, completed, now, healthy=False),
                warnings=("SOAK_HEALTH_PROBE_FAILED",),
                next_action="CONTINUE_PHASE",
            )
        if not isinstance(healthy, bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "soak health probe returned an invalid result",
            )
        resources = self._resources()
        if not healthy:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=0.0,
                checkpoint=_checkpoint(context, completed, now, healthy=False),
                resources=resources,
                warnings=("SOAK_HEALTH_NOT_READY",),
                next_action="CONTINUE_PHASE",
            )
        if context.recovering or previous_at is None or not previous_healthy:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=0.0,
                checkpoint=_checkpoint(context, completed, now, healthy=True),
                resources=resources,
                next_action="CONTINUE_PHASE",
            )

        elapsed = (now - previous_at).total_seconds()
        if elapsed < 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "soak checkpoint timestamp is ahead of owner clock",
            )
        if elapsed > self._slice_seconds:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.RUNNING,
                active_seconds=0.0,
                checkpoint=_checkpoint(context, completed, now, healthy=True),
                resources=resources,
                warnings=("SOAK_HEARTBEAT_STALE",),
                next_action="CONTINUE_PHASE",
            )

        remaining = max(0.0, target - completed)
        increment = min(elapsed, remaining)
        new_completed = completed + increment
        checkpoint = _checkpoint(context, new_completed, now, healthy=True)
        if new_completed + 1e-9 >= target:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.SUCCEEDED,
                active_seconds=increment,
                checkpoint=checkpoint,
                resources=resources,
            )
        return PhaseOutcome(
            status=PhaseOutcomeStatus.RUNNING,
            active_seconds=increment,
            checkpoint=checkpoint,
            resources=resources,
            next_action="CONTINUE_PHASE",
        )

    def _resources(self) -> tuple[ResourceSample, ...]:
        if self._sample_resources is None:
            return ()
        try:
            sample = self._sample_resources()
        except Exception:
            return ()
        if sample is None:
            return ()
        if not isinstance(sample, ResourceSample):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "resource sampler returned an invalid result",
            )
        return (sample,)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_FAILURE,
                "soak executor clock is invalid",
            )
        return value.astimezone(UTC)


def _checkpoint_state(
    context: PhaseExecutionContext,
) -> tuple[float, datetime | None, bool]:
    if context.checkpoint_token is None:
        if context.checkpoint_digest is not None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "soak checkpoint digest exists without a token",
            )
        return 0.0, None, False
    match = _SOAK_TOKEN.fullmatch(context.checkpoint_token)
    if match is None or match.group(1) != context.phase.value:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "soak checkpoint token is invalid for the current phase",
        )
    completed_milliseconds = int(match.group(2))
    checkpoint_milliseconds = int(match.group(3))
    health_state = match.group(4)
    expected = _checkpoint_digest(
        context.acceptance_run_id,
        context.phase,
        completed_milliseconds,
        checkpoint_milliseconds,
        health_state,
    )
    if context.checkpoint_digest != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "soak checkpoint digest is invalid",
        )
    completed = completed_milliseconds / 1000.0
    checkpoint_at = datetime.fromtimestamp(checkpoint_milliseconds / 1000.0, tz=UTC)
    return completed, checkpoint_at, health_state == "healthy"


def _checkpoint(
    context: PhaseExecutionContext,
    completed_seconds: float,
    now: datetime,
    *,
    healthy: bool,
) -> AcceptanceCheckpoint:
    completed_milliseconds = int(round(completed_seconds * 1000.0))
    checkpoint_milliseconds = int(now.timestamp() * 1000.0)
    health_state = "healthy" if healthy else "paused"
    token = (
        f"soak:{context.phase.value}:{completed_milliseconds}:"
        f"{checkpoint_milliseconds}:{health_state}"
    )
    return AcceptanceCheckpoint(
        phase=context.phase,
        updated_at=now,
        token=token,
        digest=_checkpoint_digest(
            context.acceptance_run_id,
            context.phase,
            completed_milliseconds,
            checkpoint_milliseconds,
            health_state,
        ),
    )


def _checkpoint_digest(
    run_id: str,
    phase: AcceptancePhase,
    completed_milliseconds: int,
    checkpoint_milliseconds: int,
    health_state: str,
) -> str:
    material = (
        f"{run_id}:{phase.value}:{completed_milliseconds}:{checkpoint_milliseconds}:{health_state}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
