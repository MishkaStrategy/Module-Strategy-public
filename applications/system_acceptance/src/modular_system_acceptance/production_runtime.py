from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    EvidenceReference,
    PhaseOutcome,
    PhaseOutcomeStatus,
)
from .native_executor import NativeOwnerPhaseExecutor
from .owner import PhaseExecutionContext
from .production_identity import TrustedAcceptanceIdentity, require_identifier, require_sha256


@dataclass(frozen=True, slots=True)
class RuntimeSmokeProof:
    """Bounded, sanitized projection of authoritative Trading Runner health."""

    runtime_id: str
    full_rc_sha: str
    config_digest: str
    execution_mode: str
    ready: bool
    live: bool
    component_count: int
    critical_failure_count: int

    def __post_init__(self) -> None:
        require_identifier(self.runtime_id, "runtime_id")
        if (
            not isinstance(self.full_rc_sha, str)
            or len(self.full_rc_sha) != 40
            or any(character not in "0123456789abcdef" for character in self.full_rc_sha)
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted runtime full_rc_sha is invalid",
            )
        require_sha256(self.config_digest, "runtime config digest")
        if self.execution_mode != "paper":
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PAPER_ONLY,
                "System Acceptance runtime proof must be PAPER-only",
            )
        if not isinstance(self.ready, bool) or not isinstance(self.live, bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted runtime health flags are invalid",
            )
        for value, name in (
            (self.component_count, "component_count"),
            (self.critical_failure_count, "critical_failure_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10_000:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    f"trusted runtime {name} is invalid",
                )
        if self.critical_failure_count > self.component_count:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "runtime critical failure count exceeds component count",
            )


class TrustedRuntimeSmokeAdapter:
    """Verify an already-running PAPER Trading Runner without owning its event loop."""

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        probe: Callable[[], RuntimeSmokeProof],
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(probe):
            raise TypeError("probe must be callable")
        self._identity = identity
        self._probe = probe

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            AcceptancePhase.RUNTIME_SMOKE,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def _verify(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not AcceptancePhase.RUNTIME_SMOKE:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "runtime smoke adapter received the wrong acceptance phase",
            )
        proof = self._probe()
        if not isinstance(proof, RuntimeSmokeProof):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "Trading Runner health owner returned an invalid proof",
            )
        if proof.full_rc_sha != self._identity.full_rc_sha:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Trading Runner full-RC identity does not match acceptance candidate",
            )
        if proof.config_digest != self._identity.config_digest:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Trading Runner configuration identity does not match acceptance candidate",
            )
        if not proof.live or not proof.ready or proof.critical_failure_count:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.BLOCKED,
                error_code=AcceptanceErrorCode.PHASE_BLOCKED.value,
                next_action="OWNER_REVIEW",
            )
        material = json.dumps(
            {
                "runtime_id": proof.runtime_id,
                "full_rc_sha": proof.full_rc_sha,
                "config_digest": proof.config_digest,
                "execution_mode": proof.execution_mode,
                "ready": proof.ready,
                "live": proof.live,
                "component_count": proof.component_count,
                "critical_failure_count": proof.critical_failure_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind="runtime.smoke",
                    digest=digest,
                    reference=f"runtime:{digest[:24]}",
                ),
            ),
        )


__all__ = ["RuntimeSmokeProof", "TrustedRuntimeSmokeAdapter"]
