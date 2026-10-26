from __future__ import annotations

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity
from modular_system_acceptance.production_verification import (
    ENVIRONMENT_CHECK_CODES,
    FINAL_CHECK_CODES,
    TrustedVerificationPhaseAdapter,
    VerificationProof,
)

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
VERIFICATION_DIGEST = "e" * 64


class Verifier:
    def __init__(
        self, proof: VerificationProof | object, *, error: Exception | None = None
    ) -> None:
        self.proof = proof
        self.error = error
        self.calls = 0

    def __call__(self) -> VerificationProof:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if not isinstance(self.proof, VerificationProof):
            return self.proof  # type: ignore[return-value]
        return self.proof


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def proof(
    phase: AcceptancePhase,
    *,
    failed: tuple[str, ...] = (),
    full_rc_sha: str = FULL_RC,
    check_codes: tuple[str, ...] | None = None,
) -> VerificationProof:
    required = (
        ENVIRONMENT_CHECK_CODES if phase is AcceptancePhase.ENVIRONMENT else FINAL_CHECK_CODES
    )
    return VerificationProof(
        full_rc_sha=full_rc_sha,
        dataset_digest=DATASET_DIGEST,
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        check_codes=tuple(sorted(required)) if check_codes is None else check_codes,
        failed_check_codes=failed,
        verification_digest=VERIFICATION_DIGEST,
    )


def context(phase: AcceptancePhase, *, full_rc_sha: str = FULL_RC) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-verification-test",
        phase=phase,
        attempt_id="attempt-verification-test",
        recovering=False,
        full_rc_sha=full_rc_sha,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        active_test_time=0.0,
        checkpoint_token=None,
        checkpoint_digest=None,
    )


@pytest.mark.parametrize(
    ("phase", "kind"),
    [
        (AcceptancePhase.ENVIRONMENT, "environment.verification"),
        (AcceptancePhase.FINAL_VERIFICATION, "final.verification"),
    ],
)
def test_verification_success_is_repeatable_and_opaque(
    phase: AcceptancePhase,
    kind: str,
) -> None:
    verifier = Verifier(proof(phase))
    current = TrustedVerificationPhaseAdapter(phase, identity=identity(), verify=verifier)

    first = current.executor.execute(context(phase))
    second = current.executor.recover(context(phase))

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert verifier.calls == 2
    assert first.evidence[0].kind == kind
    assert first.evidence[0].digest == VERIFICATION_DIGEST
    assert "dataset-v1" not in first.evidence[0].reference


def test_environment_failure_blocks_instead_of_synthetic_success() -> None:
    outcome = TrustedVerificationPhaseAdapter(
        AcceptancePhase.ENVIRONMENT,
        identity=identity(),
        verify=Verifier(proof(AcceptancePhase.ENVIRONMENT, failed=("persistence",))),
    ).executor.execute(context(AcceptancePhase.ENVIRONMENT))

    assert outcome.status is PhaseOutcomeStatus.BLOCKED
    assert outcome.error_code == AcceptanceErrorCode.PHASE_BLOCKED.value


def test_final_verification_failure_fails_acceptance() -> None:
    outcome = TrustedVerificationPhaseAdapter(
        AcceptancePhase.FINAL_VERIFICATION,
        identity=identity(),
        verify=Verifier(proof(AcceptancePhase.FINAL_VERIFICATION, failed=("recovery",))),
    ).executor.execute(context(AcceptancePhase.FINAL_VERIFICATION))

    assert outcome.status is PhaseOutcomeStatus.FAILED
    assert outcome.error_code == AcceptanceErrorCode.PHASE_FAILURE.value


def test_verification_identity_mismatch_fails_closed() -> None:
    with pytest.raises(AcceptanceOwnerError) as raised:
        TrustedVerificationPhaseAdapter(
            AcceptancePhase.ENVIRONMENT,
            identity=identity(),
            verify=Verifier(proof(AcceptancePhase.ENVIRONMENT, full_rc_sha="0" * 40)),
        ).executor.execute(context(AcceptancePhase.ENVIRONMENT))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED


def test_candidate_mismatch_blocks_before_private_verifier() -> None:
    verifier = Verifier(proof(AcceptancePhase.ENVIRONMENT))
    current = TrustedVerificationPhaseAdapter(
        AcceptancePhase.ENVIRONMENT,
        identity=identity(),
        verify=verifier,
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(AcceptancePhase.ENVIRONMENT, full_rc_sha="0" * 40))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert verifier.calls == 0


def test_missing_mandatory_check_code_requires_recovery() -> None:
    codes = tuple(sorted(ENVIRONMENT_CHECK_CODES - {"paper_only"}))
    current = TrustedVerificationPhaseAdapter(
        AcceptancePhase.ENVIRONMENT,
        identity=identity(),
        verify=Verifier(proof(AcceptancePhase.ENVIRONMENT, check_codes=codes)),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(AcceptancePhase.ENVIRONMENT))

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_invalid_verification_proof_requires_recovery() -> None:
    current = TrustedVerificationPhaseAdapter(
        AcceptancePhase.FINAL_VERIFICATION,
        identity=identity(),
        verify=Verifier(object()),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context(AcceptancePhase.FINAL_VERIFICATION))

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_verification_exception_is_sanitized() -> None:
    current = TrustedVerificationPhaseAdapter(
        AcceptancePhase.ENVIRONMENT,
        identity=identity(),
        verify=Verifier(
            proof(AcceptancePhase.ENVIRONMENT),
            error=RuntimeError("/Users/private/environment token=TOPSECRET"),
        ),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context(AcceptancePhase.ENVIRONMENT))

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    text = str(raised.value).lower()
    assert "/users/" not in text
    assert "topsecret" not in text
    assert raised.value.__cause__ is None
