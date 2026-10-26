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
from .production_identity import (
    TrustedAcceptanceIdentity,
    require_identifier,
    require_sha256,
)


@dataclass(frozen=True, slots=True)
class BacktestRunProof:
    """Sanitized proof extracted from one verified authoritative Backtest result."""

    run_id: str
    artifact_digest: str
    full_rc_sha: str
    dataset_digest: str
    strategy_digest: str
    config_digest: str
    restart_verified: bool = False
    continuous_sha256: str | None = None
    restarted_sha256: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "backtest run_id")
        require_sha256(self.artifact_digest, "backtest artifact digest")
        if (
            not isinstance(self.full_rc_sha, str)
            or len(self.full_rc_sha) != 40
            or any(character not in "0123456789abcdef" for character in self.full_rc_sha)
        ):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted backtest full_rc_sha is invalid",
            )
        require_sha256(self.dataset_digest, "backtest dataset digest")
        require_sha256(self.strategy_digest, "backtest strategy digest")
        require_sha256(self.config_digest, "backtest config digest")
        if not isinstance(self.restart_verified, bool):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "trusted backtest restart_verified is invalid",
            )
        if (self.continuous_sha256 is None) != (self.restarted_sha256 is None):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "backtest restart hashes must be both present or both absent",
            )
        if self.continuous_sha256 is not None:
            require_sha256(self.continuous_sha256, "backtest continuous digest")
            require_sha256(self.restarted_sha256, "backtest restarted digest")


@dataclass(frozen=True, slots=True)
class BacktestDeterminismProof:
    left_artifact_digest: str
    right_artifact_digest: str

    def __post_init__(self) -> None:
        require_sha256(self.left_artifact_digest, "left backtest artifact digest")
        require_sha256(self.right_artifact_digest, "right backtest artifact digest")


class TrustedBacktestRunAdapter:
    """Run BACKTEST_A/B through one trusted, idempotently published Backtest binding."""

    def __init__(
        self,
        phase: AcceptancePhase,
        *,
        identity: TrustedAcceptanceIdentity,
        run: Callable[[PhaseExecutionContext], BacktestRunProof],
    ) -> None:
        if phase not in {AcceptancePhase.BACKTEST_A, AcceptancePhase.BACKTEST_B}:
            raise ValueError("backtest run adapter requires BACKTEST_A or BACKTEST_B")
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(run):
            raise TypeError("run must be callable")
        self.phase = phase
        self._identity = identity
        self._run = run

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            self.phase,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._execute(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._execute(context)

    def _execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not self.phase:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest adapter received the wrong acceptance phase",
            )
        proof = self._run(context)
        _require_proof_identity(proof, self._identity)
        label = "a" if self.phase is AcceptancePhase.BACKTEST_A else "b"
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind=f"backtest.{label}",
                    digest=proof.artifact_digest,
                    reference=f"backtest:{label}:{proof.artifact_digest[:20]}",
                ),
            ),
        )


class TrustedBacktestDeterminismAdapter:
    """Compare two independently verified Backtest artifact identities."""

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        compare: Callable[[PhaseExecutionContext], BacktestDeterminismProof],
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(compare):
            raise TypeError("compare must be callable")
        self._identity = identity
        self._compare = compare

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            AcceptancePhase.DETERMINISM,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def _verify(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not AcceptancePhase.DETERMINISM:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest determinism adapter received the wrong acceptance phase",
            )
        proof = self._compare(context)
        if not isinstance(proof, BacktestDeterminismProof):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "Backtest determinism owner returned an invalid proof",
            )
        if proof.left_artifact_digest != proof.right_artifact_digest:
            return PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        material = json.dumps(
            {
                "left": proof.left_artifact_digest,
                "right": proof.right_artifact_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind="backtest.determinism",
                    digest=digest,
                    reference=f"determinism:{digest[:24]}",
                ),
            ),
        )


class TrustedBacktestRecoveryAdapter:
    """Verify Backtest checkpoint/restart equivalence through the authoritative owner."""

    def __init__(
        self,
        *,
        identity: TrustedAcceptanceIdentity,
        run_recovery: Callable[[PhaseExecutionContext], BacktestRunProof],
    ) -> None:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not callable(run_recovery):
            raise TypeError("run_recovery must be callable")
        self._identity = identity
        self._run_recovery = run_recovery

    @property
    def executor(self) -> NativeOwnerPhaseExecutor:
        return NativeOwnerPhaseExecutor(
            AcceptancePhase.RECOVERY,
            execute=self.execute,
            recover=self.recover,
        )

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return self._verify(context)

    def _verify(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self._identity.require_context(context)
        if context.phase is not AcceptancePhase.RECOVERY:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "Backtest recovery adapter received the wrong acceptance phase",
            )
        proof = self._run_recovery(context)
        _require_proof_identity(proof, self._identity)
        if (
            not proof.restart_verified
            or proof.continuous_sha256 is None
            or proof.restarted_sha256 is None
            or proof.continuous_sha256 != proof.restarted_sha256
        ):
            return PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code=AcceptanceErrorCode.PHASE_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        material = json.dumps(
            {
                "artifact_digest": proof.artifact_digest,
                "continuous_sha256": proof.continuous_sha256,
                "restarted_sha256": proof.restarted_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return PhaseOutcome(
            status=PhaseOutcomeStatus.SUCCEEDED,
            evidence=(
                EvidenceReference(
                    kind="backtest.recovery",
                    digest=digest,
                    reference=f"recovery:{digest[:24]}",
                ),
            ),
        )


def _require_proof_identity(
    proof: object,
    identity: TrustedAcceptanceIdentity,
) -> BacktestRunProof:
    if not isinstance(proof, BacktestRunProof):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "Backtest owner returned an invalid run proof",
        )
    observed = (
        proof.full_rc_sha,
        proof.dataset_digest,
        proof.strategy_digest,
        proof.config_digest,
    )
    expected = (
        identity.full_rc_sha,
        identity.dataset_digest,
        identity.strategy_digest,
        identity.config_digest,
    )
    if observed != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest proof identity does not match the durable acceptance candidate",
        )
    return proof


__all__ = [
    "BacktestDeterminismProof",
    "BacktestRunProof",
    "TrustedBacktestDeterminismAdapter",
    "TrustedBacktestRecoveryAdapter",
    "TrustedBacktestRunAdapter",
]
