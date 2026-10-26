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
from modular_system_acceptance.production_runtime import (
    RuntimeSmokeProof,
    TrustedRuntimeSmokeAdapter,
)

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64


class Probe:
    def __init__(
        self, proof: RuntimeSmokeProof | object, *, error: Exception | None = None
    ) -> None:
        self.proof = proof
        self.error = error
        self.calls = 0

    def __call__(self) -> RuntimeSmokeProof:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if not isinstance(self.proof, RuntimeSmokeProof):
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


def context(*, full_rc_sha: str = FULL_RC) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-runtime-test",
        phase=AcceptancePhase.RUNTIME_SMOKE,
        attempt_id="attempt-runtime-test",
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
    full_rc_sha: str = FULL_RC,
    config_digest: str = CONFIG_DIGEST,
    ready: bool = True,
    live: bool = True,
    critical_failure_count: int = 0,
) -> RuntimeSmokeProof:
    return RuntimeSmokeProof(
        runtime_id="paper-runtime-v1",
        full_rc_sha=full_rc_sha,
        config_digest=config_digest,
        execution_mode="paper",
        ready=ready,
        live=live,
        component_count=4,
        critical_failure_count=critical_failure_count,
    )


def test_runtime_smoke_success_is_read_only_and_repeatable() -> None:
    probe = Probe(proof())
    current = TrustedRuntimeSmokeAdapter(identity=identity(), probe=probe)

    first = current.executor.execute(context())
    second = current.executor.recover(context())

    assert first.status is PhaseOutcomeStatus.SUCCEEDED
    assert second == first
    assert probe.calls == 2
    assert first.evidence[0].kind == "runtime.smoke"
    assert first.evidence[0].reference.startswith("runtime:")
    assert "paper-runtime-v1" not in first.evidence[0].reference


@pytest.mark.parametrize(
    "value",
    [
        proof(ready=False),
        proof(live=False),
        proof(critical_failure_count=1),
    ],
)
def test_runtime_not_ready_or_not_live_blocks_acceptance(value: RuntimeSmokeProof) -> None:
    outcome = TrustedRuntimeSmokeAdapter(
        identity=identity(),
        probe=Probe(value),
    ).executor.execute(context())

    assert outcome.status is PhaseOutcomeStatus.BLOCKED
    assert outcome.error_code == AcceptanceErrorCode.PHASE_BLOCKED.value


@pytest.mark.parametrize(
    "value",
    [
        proof(full_rc_sha="0" * 40),
        proof(config_digest="0" * 64),
    ],
)
def test_runtime_proof_identity_mismatch_fails_closed(value: RuntimeSmokeProof) -> None:
    with pytest.raises(AcceptanceOwnerError) as raised:
        TrustedRuntimeSmokeAdapter(
            identity=identity(),
            probe=Probe(value),
        ).executor.execute(context())

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED


def test_acceptance_candidate_mismatch_blocks_before_runtime_probe() -> None:
    runtime_probe = Probe(proof())
    current = TrustedRuntimeSmokeAdapter(identity=identity(), probe=runtime_probe)

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.execute(context(full_rc_sha="0" * 40))

    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED
    assert runtime_probe.calls == 0


def test_runtime_proof_enforces_paper_mode_at_construction() -> None:
    with pytest.raises(AcceptanceOwnerError) as raised:
        RuntimeSmokeProof(
            runtime_id="runtime-live-v1",
            full_rc_sha=FULL_RC,
            config_digest=CONFIG_DIGEST,
            execution_mode="live",
            ready=True,
            live=True,
            component_count=1,
            critical_failure_count=0,
        )

    assert raised.value.code is AcceptanceErrorCode.PAPER_ONLY


def test_invalid_runtime_proof_requires_recovery() -> None:
    current = TrustedRuntimeSmokeAdapter(identity=identity(), probe=Probe(object()))

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_runtime_health_exception_is_sanitized() -> None:
    current = TrustedRuntimeSmokeAdapter(
        identity=identity(),
        probe=Probe(proof(), error=RuntimeError("/Users/private/runtime token=TOPSECRET")),
    )

    with pytest.raises(AcceptanceOwnerError) as raised:
        current.executor.recover(context())

    assert raised.value.code is AcceptanceErrorCode.OWNER_FAILURE
    text = str(raised.value).lower()
    assert "/users/" not in text
    assert "topsecret" not in text
    assert raised.value.__cause__ is None
