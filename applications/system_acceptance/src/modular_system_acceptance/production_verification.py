from __future__ import annotations

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

ENVIRONMENT_CHECK_CODES = frozenset(
    {
        "candidate",
        "persistence",
        "dataset_owner",
        "backtest_owner",
        "research_owner",
        "runtime_owner",
        "paper_only",
    }
)
FINAL_CHECK_CODES = frozenset(
    {
        "candidate",
        "dataset",
        "backtest_a",
        "backtest_b",
        "determinism",
        "research",
        "runtime_smoke",
        "recovery",
        "evidence",
    }
)


@dataclass(frozen=True, slots=True)
class VerificationProof:
    """Sanitized aggregate proof over deployment-private verification checks."""

    full_rc_sha: str
    dataset_digest: str
    strategy_digest: str
    config_digest: str
    check_codes: tuple[str, ...]
    failed_check_codes: tuple[str, ...]
    verification_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.full_rc_sha, str)
            or len(self.full_rc_sha) != 40
            or any(character not in "0123456789abcdef" for character in self.full_rc_sha)
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted verification full_rc_sha is invalid",
            )
        require_sha256(self.dataset_digest, "verification dataset digest")
        require_sha256(self.strategy_digest, "verification strategy digest")
        require_sha256(self.config_digest, "verification config digest")
        checks = _codes(self.check_codes, "verification check_codes")
        failed = _codes(
            self.failed_check_codes, "verification failed_check_codes", allow_empty=True
        )
        if not set(failed).issubset(checks):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "failed verification checks must be part of check_codes",
            )
        object.__setattr__(self, "check_codes", tuple(sorted(checks)))
        object.__setattr__(self, "failed_check_codes", tuple(sorted(failed)))
        require_sha256(self.verification_digest, "verification digest")


class TrustedVerificationPhaseAdapter:
    """Bind ENVIRONMENT/FINAL_VERIFICATION to repeatable private owner checks."""

    def __init__(
        self,
        phase: AcceptancePhase,
        *,
        identity: TrustedAcceptanceIdentity,
        verify: Callable[[], VerificationProof],
    ) -> None:
        if phase not in {AcceptancePhase.ENVIRONMENT, AcceptancePhase.FINAL_VERIFICATION}:
            raise ValueError("verification adapter requires ENVIRONMENT or FINAL_VERIFICATION")
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(verify):
            raise TypeError("verify must be callable")
        self.phase = phase
        self._identity = identity
        self._verify_callback = verify

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            self.phase,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def _verify(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not self.phase:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "verification adapter received the wrong acceptance phase",
            )
        proof = self._verify_callback()
        if not isinstance(proof, VerificationProof):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "verification owner returned an invalid proof",
            )
        observed = (
            proof.full_rc_sha,
            proof.dataset_digest,
            proof.strategy_digest,
            proof.config_digest,
        )
        expected = (
            self._identity.full_rc_sha,
            self._identity.dataset_digest,
            self._identity.strategy_digest,
            self._identity.config_digest,
        )
        if observed != expected:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "verification proof identity does not match acceptance candidate",
            )
        required_codes = (
            ENVIRONMENT_CHECK_CODES
            if self.phase is AcceptancePhase.ENVIRONMENT
            else FINAL_CHECK_CODES
        )
        if set(proof.check_codes) != set(required_codes):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "verification proof does not cover the required owner checks",
            )
        if proof.failed_check_codes:
            return PhaseOutcome(
                status=(
                    PhaseOutcomeStatus.BLOCKED
                    if self.phase is AcceptancePhase.ENVIRONMENT
                    else PhaseOutcomeStatus.FAILED
                ),
                error_code=(
                    AcceptanceErrorCode.PHASE_BLOCKED.value
                    if self.phase is AcceptancePhase.ENVIRONMENT
                    else AcceptanceErrorCode.PHASE_FAILURE.value
                ),
                next_action="OWNER_REVIEW",
            )
        kind = (
            "environment.verification"
            if self.phase is AcceptancePhase.ENVIRONMENT
            else "final.verification"
        )
        reference_prefix = "environment" if self.phase is AcceptancePhase.ENVIRONMENT else "final"
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind=kind,
                    digest=proof.verification_digest,
                    reference=f"{reference_prefix}:{proof.verification_digest[:24]}",
                ),
            ),
        )


def _codes(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, tuple):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"trusted {name} is invalid",
        )
    if not value and not allow_empty:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"trusted {name} must not be empty",
        )
    normalized = tuple(require_identifier(item, name) for item in value)
    if len(normalized) != len(set(normalized)):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"trusted {name} contains duplicates",
        )
    return normalized


__all__ = [
    "ENVIRONMENT_CHECK_CODES",
    "FINAL_CHECK_CODES",
    "TrustedVerificationPhaseAdapter",
    "VerificationProof",
]
