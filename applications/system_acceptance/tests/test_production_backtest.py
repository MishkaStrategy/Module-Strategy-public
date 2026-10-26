from __future__ import annotations

from collections.abc import Callable

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.production_backtest import (
    BacktestDeterminismProof,
    BacktestRunProof,
    TrustedBacktestDeterminismAdapter,
    TrustedBacktestRecoveryAdapter,
    TrustedBacktestRunAdapter,
)
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
ARTIFACT_DIGEST = "e" * 64
RESTART_DIGEST = "f" * 64


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def context(
    phase: AcceptancePhase,
    *,
    full_rc_sha: str = FULL_RC,
    attempt_id: str = "attempt-backtest-test",
) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-backtest-test",
        phase=phase,
        attempt_id=attempt_id,
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


def proof(
    *,
    artifact_digest: str = ARTIFACT_DIGEST,
    full_rc_sha: str = FULL_RC,
    dataset_digest: str = DATASET_DIGEST,
    strategy_digest: str = STRATEGY_DIGEST,
    config_digest: str = CONFIG_DIGEST,
    restart_verified: bool = False,
    continuous_sha256: str | None = None,
    restarted_sha256: str | None = None,
) -> BacktestRunProof:
    return BacktestRunProof(
        run_id="backtest-run-v1",
        artifact_digest=artifact_digest,
        full_rc_sha=full_rc_sha,
        dataset_digest=dataset_digest,
        strategy_digest=strategy_digest,
        config_digest=config_digest,
        restart_verified=restart_verified,
        continuous_sha256=continuous_sha256,
        restarted_sha256=restarted_sha256,
    )


class RunBinding:
    def __init__(
        self,
        value: BacktestRunProof | object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.value = value
        self.error = error
        self.attempt_ids: list[str] = []

    def __call__(self, current: PhaseExecutionContext) -> BacktestRunProof:
        self.attempt_ids.append(current.attempt_id)
        if self.error is not None:
            raise self.error
        if not isinstance(self.value, BacktestRunProof):
            return self.value  # type: ignore[return-value]
        return self.value


class DeterminismBinding:
    def __init__(self, value: BacktestDeterminismProof) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, current: PhaseExecutionContext) -> BacktestDeterminismProof:
        assert current.phase is AcceptancePhase.DETERMINISM
        self.calls += 1
        return self.value


def test_backtest_a_execute_and_recovery_reuse_same_attempt_identity() -> None:
    binding = RunBinding(proof())
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_A,
        identity=identity(),
        run=binding,
    )
    phase_context = context(AcceptancePhase.BACKTEST_A, attempt_id="attempt-stable-a")

    first = current.executor.execute(phase_context)
    second = current.executor.recover(phase_context)

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert binding.attempt_ids == ["attempt-stable-a", "attempt-stable-a"]
    assert first.evidence[0].kind == "backtest.a"
    assert first.evidence[0].digest == ARTIFACT_DIGEST


def test_backtest_b_has_distinct_evidence_kind() -> None:
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_B,
        identity=identity(),
        run=RunBinding(proof()),
    )

    outcome = current.executor.execute(context(AcceptancePhase.BACKTEST_B))

    assert outcome.status is PhaseOutcomeStatus.SUCCEEDED
    assert outcome.evidence[0].kind == "backtest.b"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: proof(full_rc_sha="0" * 40),
        lambda: proof(dataset_digest="0" * 64),
        lambda: proof(strategy_digest="0" * 64),
        lambda: proof(config_digest="0" * 64),
    ],
)
def test_backtest_proof_identity_mismatch_fails_closed(
    factory: Callable[[], BacktestRunProof],
) -> None:
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_A,
        identity=identity(),
        run=RunBinding(factory()),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(AcceptancePhase.BACKTEST_A))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED


def test_candidate_mismatch_blocks_before_backtest_binding() -> None:
    binding = RunBinding(proof())
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_A,
        identity=identity(),
        run=binding,
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(AcceptancePhase.BACKTEST_A, full_rc_sha="0" * 40))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert binding.attempt_ids == []


def test_invalid_backtest_owner_proof_requires_recovery() -> None:
    binding = RunBinding(object())
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_A,
        identity=identity(),
        run=binding,
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(AcceptancePhase.BACKTEST_A))

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_determinism_requires_identical_verified_artifact_digests() -> None:
    success_binding = DeterminismBinding(BacktestDeterminismProof(ARTIFACT_DIGEST, ARTIFACT_DIGEST))
    success = TrustedBacktestDeterminismAdapter(
        identity=identity(),
        compare=success_binding,
    ).executor.execute(context(AcceptancePhase.DETERMINISM))
    assert success.status is PhaseOutcomeStatus.SUCCEEDED
    assert success.evidence[0].kind == "backtest.determinism"

    failed = TrustedBacktestDeterminismAdapter(
        identity=identity(),
        compare=DeterminismBinding(BacktestDeterminismProof(ARTIFACT_DIGEST, "1" * 64)),
    ).executor.recover(context(AcceptancePhase.DETERMINISM))
    assert failed.status is PhaseOutcomeStatus.FAILED
    assert failed.error_code == AcceptanceErrorCode.PHASE_FAILURE.value


def test_recovery_requires_verified_equal_continuous_and_restart_hashes() -> None:
    binding = RunBinding(
        proof(
            restart_verified=True,
            continuous_sha256=RESTART_DIGEST,
            restarted_sha256=RESTART_DIGEST,
        )
    )
    current = TrustedBacktestRecoveryAdapter(identity=identity(), run_recovery=binding)
    phase_context = context(AcceptancePhase.RECOVERY, attempt_id="attempt-recovery-stable")

    first = current.executor.execute(phase_context)
    second = current.executor.recover(phase_context)

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert binding.attempt_ids == ["attempt-recovery-stable", "attempt-recovery-stable"]
    assert first.evidence[0].kind == "backtest.recovery"


@pytest.mark.parametrize(
    "value",
    [
        proof(restart_verified=False),
        proof(
            restart_verified=True,
            continuous_sha256=RESTART_DIGEST,
            restarted_sha256="1" * 64,
        ),
    ],
)
def test_recovery_rejects_unverified_or_divergent_restart(value: BacktestRunProof) -> None:
    outcome = TrustedBacktestRecoveryAdapter(
        identity=identity(),
        run_recovery=RunBinding(value),
    ).executor.execute(context(AcceptancePhase.RECOVERY))

    assert outcome.status is PhaseOutcomeStatus.FAILED
    assert outcome.error_code == AcceptanceErrorCode.PHASE_FAILURE.value


def test_backtest_owner_exception_is_sanitized() -> None:
    binding = RunBinding(
        proof(),
        error=RuntimeError("/Users/private/backtest token=TOPSECRET"),
    )
    current = TrustedBacktestRunAdapter(
        AcceptancePhase.BACKTEST_A,
        identity=identity(),
        run=binding,
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context(AcceptancePhase.BACKTEST_A))

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    text = str(raised.value).lower()
    assert "/users/" not in text
    assert "topsecret" not in text
    assert raised.value.__cause__ is None
