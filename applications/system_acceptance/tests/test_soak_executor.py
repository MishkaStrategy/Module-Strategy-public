from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_system_acceptance import (
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcomeStatus,
    ResourceSample,
)
from modular_system_acceptance.executors import CheckpointedSoakExecutor

NOW = datetime(2026, 8, 8, 23, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def context(
    *,
    phase: AcceptancePhase = AcceptancePhase.SOAK_2H,
    token: str | None = None,
    digest: str | None = None,
    recovering: bool = False,
) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        acceptance_run_id="acceptance-soak-test",
        phase=phase,
        attempt_id="attempt-soak-test",
        recovering=recovering,
        full_rc_sha="a" * 40,
        dataset_id="dataset-v1",
        dataset_digest="b" * 64,
        strategy_id="strategy-v1",
        strategy_digest="c" * 64,
        config_digest="d" * 64,
        active_test_time=0.0,
        checkpoint_token=token,
        checkpoint_digest=digest,
    )


def targets(seconds: float = 2.0) -> dict[AcceptancePhase, float]:
    return {
        AcceptancePhase.SOAK_2H: seconds,
        AcceptancePhase.SOAK_24H: seconds,
        AcceptancePhase.SOAK_72H: seconds,
    }


def test_healthy_heartbeats_credit_real_elapsed_time_and_finish_at_target() -> None:
    clock = Clock()
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=clock.now,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )

    first = executor.execute(context())
    assert first.status is PhaseOutcomeStatus.RUNNING
    assert first.active_seconds == 0.0
    assert first.checkpoint is not None
    assert first.checkpoint.token.endswith(":healthy")

    clock.advance(1.0)
    second = executor.execute(context(token=first.checkpoint.token, digest=first.checkpoint.digest))
    assert second.status is PhaseOutcomeStatus.RUNNING
    assert second.active_seconds == 1.0
    assert second.checkpoint is not None
    assert ":1000:" in second.checkpoint.token

    clock.advance(1.0)
    third = executor.execute(
        context(token=second.checkpoint.token, digest=second.checkpoint.digest)
    )
    assert third.status is PhaseOutcomeStatus.SUCCEEDED
    assert third.active_seconds == 1.0
    assert third.checkpoint is not None
    assert ":2000:" in third.checkpoint.token


def test_unhealthy_transition_requires_fresh_healthy_baseline_before_credit() -> None:
    clock = Clock()
    health = {"ready": False}
    executor = CheckpointedSoakExecutor(
        lambda _: health["ready"],
        clock=clock.now,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )

    unhealthy = executor.execute(context())
    assert unhealthy.active_seconds == 0.0
    assert unhealthy.warnings == ("SOAK_HEALTH_NOT_READY",)
    assert unhealthy.checkpoint is not None
    assert unhealthy.checkpoint.token.endswith(":paused")

    clock.advance(0.5)
    health["ready"] = True
    baseline = executor.execute(
        context(token=unhealthy.checkpoint.token, digest=unhealthy.checkpoint.digest)
    )
    assert baseline.active_seconds == 0.0
    assert baseline.checkpoint is not None
    assert baseline.checkpoint.token.endswith(":healthy")

    clock.advance(0.5)
    credited = executor.execute(
        context(token=baseline.checkpoint.token, digest=baseline.checkpoint.digest)
    )
    assert credited.active_seconds == 0.5


def test_probe_failure_does_not_credit_or_expose_private_error() -> None:
    def failure(_: PhaseExecutionContext) -> bool:
        raise RuntimeError("private host detail")

    failed_probe = CheckpointedSoakExecutor(
        failure,
        clock=lambda: NOW,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )
    failed = failed_probe.execute(context())
    assert failed.active_seconds == 0.0
    assert failed.warnings == ("SOAK_HEALTH_PROBE_FAILED",)
    assert failed.checkpoint is not None
    assert failed.checkpoint.token.endswith(":paused")
    assert "private host detail" not in repr(failed)


def test_recovery_rebases_heartbeat_without_crediting_downtime() -> None:
    clock = Clock()
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=clock.now,
        slice_seconds=1.0,
        target_seconds=targets(3.0),
    )

    baseline = executor.execute(context())
    assert baseline.checkpoint is not None
    clock.advance(1.0)
    credited = executor.execute(
        context(token=baseline.checkpoint.token, digest=baseline.checkpoint.digest)
    )
    assert credited.active_seconds == 1.0
    assert credited.checkpoint is not None

    clock.advance(11 * 60 * 60)
    recovered = executor.recover(
        context(
            token=credited.checkpoint.token,
            digest=credited.checkpoint.digest,
            recovering=True,
        )
    )
    assert recovered.active_seconds == 0.0
    assert recovered.status is PhaseOutcomeStatus.RUNNING
    assert recovered.checkpoint is not None
    assert ":1000:" in recovered.checkpoint.token

    clock.advance(1.0)
    after_recovery = executor.execute(
        context(token=recovered.checkpoint.token, digest=recovered.checkpoint.digest)
    )
    assert after_recovery.active_seconds == 1.0
    assert after_recovery.checkpoint is not None
    assert ":2000:" in after_recovery.checkpoint.token


def test_stale_heartbeat_gap_contributes_zero_active_time() -> None:
    clock = Clock()
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=clock.now,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )

    baseline = executor.execute(context())
    assert baseline.checkpoint is not None
    clock.advance(5.0)
    stale = executor.execute(
        context(token=baseline.checkpoint.token, digest=baseline.checkpoint.digest)
    )
    assert stale.active_seconds == 0.0
    assert stale.warnings == ("SOAK_HEARTBEAT_STALE",)
    assert stale.checkpoint is not None
    assert ":0:" in stale.checkpoint.token

    clock.advance(1.0)
    resumed = executor.execute(
        context(token=stale.checkpoint.token, digest=stale.checkpoint.digest)
    )
    assert resumed.active_seconds == 1.0


def test_tampered_checkpoint_fails_closed() -> None:
    clock = Clock()
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=clock.now,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )
    valid = executor.execute(context())
    assert valid.checkpoint is not None
    parts = valid.checkpoint.token.split(":")
    parts[-2] = str(int(parts[-2]) + 1)
    tampered_token = ":".join(parts)

    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.recover(
            context(
                token=tampered_token,
                digest=valid.checkpoint.digest,
                recovering=True,
            )
        )
    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_checkpoint_digest_without_token_fails_closed() -> None:
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=lambda: NOW,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.execute(context(digest="f" * 64))
    assert raised.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED


def test_non_soak_phase_is_rejected() -> None:
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        target_seconds=targets(),
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        executor.execute(context(phase=AcceptancePhase.ENVIRONMENT))
    assert raised.value.code is AcceptanceErrorCode.PHASE_BLOCKED


def test_resource_sampling_is_bounded_typed_and_optional() -> None:
    sample = ResourceSample(
        timestamp=NOW,
        cpu_percent=10.0,
        ram_bytes=100,
        disk_free_bytes=1000,
    )
    executor = CheckpointedSoakExecutor(
        lambda _: True,
        clock=lambda: NOW,
        sample_resources=lambda: sample,
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )
    outcome = executor.execute(context())
    assert outcome.resources == (sample,)

    invalid = CheckpointedSoakExecutor(
        lambda _: True,
        clock=lambda: NOW,
        sample_resources=lambda: "bad",  # type: ignore[return-value]
        slice_seconds=1.0,
        target_seconds=targets(2.0),
    )
    with pytest.raises(AcceptanceOwnerError) as raised:
        invalid.execute(context())
    assert raised.value.code is AcceptanceErrorCode.PHASE_FAILURE
