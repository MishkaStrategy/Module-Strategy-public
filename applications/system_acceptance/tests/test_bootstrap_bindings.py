from __future__ import annotations

from dataclasses import dataclass

from modular_system_acceptance import AcceptancePhase
from modular_system_acceptance.bootstrap import SystemAcceptanceProductionBindings
from modular_system_acceptance.executors import CheckpointedSoakExecutor
from modular_system_acceptance.production_backtest import BacktestDeterminismProof, BacktestRunProof
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity
from modular_system_acceptance.production_runtime import RuntimeSmokeProof
from modular_system_acceptance.production_verification import (
    ENVIRONMENT_CHECK_CODES,
    FINAL_CHECK_CODES,
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


def bindings() -> SystemAcceptanceProductionBindings:
    return SystemAcceptanceProductionBindings(
        environment_verify=lambda: verification(AcceptancePhase.ENVIRONMENT),
        dataset_resolver=Selection,
        dataset_version=1,
        dataset_timeframe="5m",
        backtest_a=lambda _: run_proof(),
        backtest_b=lambda _: run_proof(),
        determinism_compare=lambda _: BacktestDeterminismProof(
            ARTIFACT_DIGEST,
            ARTIFACT_DIGEST,
        ),
        research_runner=Runner(),
        research_prepared=Prepared(),
        research_specification_fingerprint=RESEARCH_DIGEST,
        runtime_probe=lambda: RuntimeSmokeProof(
            runtime_id="paper-runtime-v1",
            full_rc_sha=FULL_RC,
            config_digest=CONFIG_DIGEST,
            execution_mode="paper",
            ready=True,
            live=True,
            component_count=2,
            critical_failure_count=0,
        ),
        recovery_run=lambda _: run_proof(recovery=True),
        final_verify=lambda: verification(AcceptancePhase.FINAL_VERIFICATION),
    )


def test_bindings_build_complete_production_registry() -> None:
    current = bindings()
    phases = current.build(identity())
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


def test_bindings_repr_does_not_expose_runtime_objects() -> None:
    current = bindings()
    rendered = repr(current)
    assert "Runner" not in rendered
    assert "Selection" not in rendered
    assert "lambda" not in rendered
    assert RESEARCH_DIGEST in rendered
