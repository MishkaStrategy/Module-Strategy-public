from __future__ import annotations

from dataclasses import dataclass, replace as dataclass_replace

import pytest

from modular_system_acceptance import AcceptancePhase
from modular_system_acceptance.executors import CheckpointedSoakExecutor
from modular_system_acceptance.production_backtest import (
    BacktestDeterminismProof,
    BacktestRunProof,
    TrustedBacktestDeterminismAdapter,
    TrustedBacktestRecoveryAdapter,
    TrustedBacktestRunAdapter,
)
from modular_system_acceptance.production_composition import TrustedProductionPhaseAdapters
from modular_system_acceptance.production_dataset import TrustedDatasetPhaseAdapter
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity
from modular_system_acceptance.production_research import TrustedResearchPhaseAdapter
from modular_system_acceptance.production_runtime import (
    RuntimeSmokeProof,
    TrustedRuntimeSmokeAdapter,
)
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
RESEARCH_DIGEST = "e" * 64
ARTIFACT_DIGEST = "f" * 64


@dataclass(frozen=True)
class Descriptor:
    dataset_id: str = "dataset-v1"
    version: int = 1
    dataset_hash: str = DATASET_DIGEST


@dataclass(frozen=True)
class Selection:
    descriptor: Descriptor = Descriptor()
    timeframe: str = "5m"
    row_count: int = 100


@dataclass(frozen=True)
class Specification:
    fingerprint: str = RESEARCH_DIGEST


@dataclass(frozen=True)
class Manifest:
    experiment_id: str = "experiment-v1"


@dataclass(frozen=True)
class Prepared:
    specification: Specification = Specification()
    manifest: Manifest = Manifest()


@dataclass(frozen=True)
class Summary:
    status: object = "COMPLETED"


class Repository:
    def experiment_status(self, experiment_id: str) -> object:
        assert experiment_id == "experiment-v1"
        return "COMPLETED"


class Runner:
    def __init__(self) -> None:
        self.repository = Repository()

    def execute(self, experiment_id: str) -> Summary:
        assert experiment_id == "experiment-v1"
        return Summary()


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def run_proof(*, recovery: bool = False) -> BacktestRunProof:
    restart_digest = "1" * 64 if recovery else None
    return BacktestRunProof(
        run_id="backtest-run-v1",
        artifact_digest=ARTIFACT_DIGEST,
        full_rc_sha=FULL_RC,
        dataset_digest=DATASET_DIGEST,
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        restart_verified=recovery,
        continuous_sha256=restart_digest,
        restarted_sha256=restart_digest,
    )


def verification(phase: AcceptancePhase) -> VerificationProof:
    checks = ENVIRONMENT_CHECK_CODES if phase is AcceptancePhase.ENVIRONMENT else FINAL_CHECK_CODES
    return VerificationProof(
        full_rc_sha=FULL_RC,
        dataset_digest=DATASET_DIGEST,
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
        check_codes=tuple(sorted(checks)),
        failed_check_codes=(),
        verification_digest="2" * 64,
    )


def _phase_adapters(candidate: TrustedAcceptanceIdentity) -> TrustedProductionPhaseAdapters:
    return TrustedProductionPhaseAdapters(
        environment=TrustedVerificationPhaseAdapter(
            AcceptancePhase.ENVIRONMENT,
            identity=candidate,
            verify=lambda: verification(AcceptancePhase.ENVIRONMENT),
        ),
        dataset=TrustedDatasetPhaseAdapter(
            identity=candidate,
            resolver=Selection,
            version=1,
            timeframe="5m",
        ),
        backtest_a=TrustedBacktestRunAdapter(
            AcceptancePhase.BACKTEST_A,
            identity=candidate,
            run=lambda _: run_proof(),
        ),
        backtest_b=TrustedBacktestRunAdapter(
            AcceptancePhase.BACKTEST_B,
            identity=candidate,
            run=lambda _: run_proof(),
        ),
        determinism=TrustedBacktestDeterminismAdapter(
            identity=candidate,
            compare=lambda _: BacktestDeterminismProof(ARTIFACT_DIGEST, ARTIFACT_DIGEST),
        ),
        research=TrustedResearchPhaseAdapter(
            identity=candidate,
            runner=Runner(),
            prepared=Prepared(),
            specification_fingerprint=RESEARCH_DIGEST,
        ),
        runtime_smoke=TrustedRuntimeSmokeAdapter(
            identity=candidate,
            probe=lambda: RuntimeSmokeProof(
                runtime_id="paper-runtime-v1",
                full_rc_sha=FULL_RC,
                config_digest=CONFIG_DIGEST,
                execution_mode="paper",
                ready=True,
                live=True,
                component_count=2,
                critical_failure_count=0,
            ),
        ),
        recovery=TrustedBacktestRecoveryAdapter(
            identity=candidate,
            run_recovery=lambda _: run_proof(recovery=True),
        ),
        final_verification=TrustedVerificationPhaseAdapter(
            AcceptancePhase.FINAL_VERIFICATION,
            identity=candidate,
            verify=lambda: verification(AcceptancePhase.FINAL_VERIFICATION),
        ),
    )


def test_typed_production_composition_builds_every_acceptance_phase() -> None:
    phases = _phase_adapters(identity())
    soak = CheckpointedSoakExecutor(lambda _: True)

    registry = phases.build_registry(soak=soak)

    for phase in AcceptancePhase:
        executor = registry.for_phase(phase)
        if phase in {
            AcceptancePhase.SOAK_2H,
            AcceptancePhase.SOAK_24H,
            AcceptancePhase.SOAK_72H,
        }:
            assert executor is soak
        else:
            assert executor is not soak


def test_typed_production_composition_rejects_mixed_candidate_identity() -> None:
    candidate = identity()
    phases = _phase_adapters(candidate)
    mixed_identity = dataclass_replace(candidate, dataset_digest="0" * 64)
    mixed_dataset = TrustedDatasetPhaseAdapter(
        identity=mixed_identity,
        resolver=Selection,
        version=1,
        timeframe="5m",
    )

    with pytest.raises(ValueError, match="share one acceptance identity"):
        dataclass_replace(phases, dataset=mixed_dataset)


def test_registry_build_rejects_identity_drift_after_composition() -> None:
    candidate = identity()
    phases = _phase_adapters(candidate)
    phases.dataset._identity = dataclass_replace(candidate, dataset_digest="0" * 64)

    with pytest.raises(ValueError, match="share one acceptance identity"):
        phases.build_registry(soak=CheckpointedSoakExecutor(lambda _: True))
