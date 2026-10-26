from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
)
from modular_system_acceptance.bootstrap import (
    ProductionSystemAcceptanceProcess,
    StagedPinnedSystemAcceptanceHttpServer,
    SystemAcceptanceBootstrapConfig,
)
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
READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


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


class Executor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


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


def phases(candidate: TrustedAcceptanceIdentity) -> TrustedProductionPhaseAdapters:
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


def config(path: Path, *, allow_soak_24h: bool = False) -> SystemAcceptanceBootstrapConfig:
    return SystemAcceptanceBootstrapConfig(
        identity=identity(),
        state_path=path,
        instance_id="bootstrap-owner",
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
        poll_seconds=0.05,
        lease_seconds=5,
        allow_soak_24h=allow_soak_24h,
    )


def start_body(*, soak_24h: bool = False, soak_72h: bool = False) -> bytes:
    return json.dumps(
        {
            "request_id": "bootstrap-start",
            "spec": {
                "dataset_id": "dataset-v1",
                "dataset_digest": DATASET_DIGEST,
                "strategy_id": "strategy-v1",
                "strategy_digest": STRATEGY_DIGEST,
                "config_digest": CONFIG_DIGEST,
                "enable_soak_24h": soak_24h,
                "enable_soak_72h": soak_72h,
            },
        }
    ).encode()


def staged_server(
    path: Path, *, allow_soak_24h: bool
) -> tuple[StagedPinnedSystemAcceptanceHttpServer, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="staged-http-owner",
        lease_seconds=5,
    )
    server = StagedPinnedSystemAcceptanceHttpServer(
        owner,
        full_rc_sha=FULL_RC,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
        allow_soak_24h=allow_soak_24h,
    )
    return server, store


def test_bootstrap_config_redacts_private_path_and_tokens(tmp_path: Path) -> None:
    current = config(tmp_path / "private" / "acceptance.db")
    rendered = repr(current)
    assert READER not in rendered
    assert OPERATOR not in rendered
    assert str(current.state_path) not in rendered
    assert FULL_RC in rendered


def test_bootstrap_config_rejects_shared_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        SystemAcceptanceBootstrapConfig(
            identity=identity(),
            state_path=tmp_path / "acceptance.db",
            reader_token=READER,
            operator_token=READER,
        )


def test_process_starts_once_on_loopback_and_closes_terminally(tmp_path: Path) -> None:
    candidate = identity()
    process = ProductionSystemAcceptanceProcess(
        config=config(tmp_path / "acceptance.db"),
        phases=phases(candidate),
        soak=CheckpointedSoakExecutor(lambda _: True),
    )
    process.start()
    try:
        process.start()
        assert process.running is True
        assert process.address[0] == "127.0.0.1"
    finally:
        process.close()
    assert process.running is False
    process.close()


def test_baseline_start_pins_full_rc_and_paper(tmp_path: Path) -> None:
    candidate = identity()
    process = ProductionSystemAcceptanceProcess(
        config=config(tmp_path / "acceptance.db"),
        phases=phases(candidate),
        soak=CheckpointedSoakExecutor(lambda _: True),
    )
    try:
        code, _, _ = process._http.response(  # noqa: SLF001 - verifies bootstrap transport
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body(),
        )
        assert code == 200
        snapshot = process.owner.snapshot()
        assert snapshot is not None
        assert snapshot.spec.full_rc_sha == FULL_RC
        assert snapshot.spec.mode == "PAPER"
        assert snapshot.spec.enable_soak_24h is False
        assert snapshot.spec.enable_soak_72h is False
    finally:
        process.close()


def test_staged_policy_blocks_24h_until_deployment_opt_in(tmp_path: Path) -> None:
    server, store = staged_server(tmp_path / "blocked-24h.db", allow_soak_24h=False)
    try:
        code, _, _ = server.response(
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body(soak_24h=True),
        )
        assert code == 400
        assert server._owner.snapshot() is None  # noqa: SLF001
    finally:
        server.close()
        store.close()


def test_staged_policy_allows_explicit_24h_opt_in(tmp_path: Path) -> None:
    server, store = staged_server(tmp_path / "allowed-24h.db", allow_soak_24h=True)
    try:
        code, _, _ = server.response(
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body(soak_24h=True),
        )
        assert code == 200
        snapshot = server._owner.snapshot()  # noqa: SLF001
        assert snapshot is not None
        assert snapshot.spec.enable_soak_24h is True
    finally:
        server.close()
        store.close()


def test_staged_policy_always_blocks_72h(tmp_path: Path) -> None:
    server, store = staged_server(tmp_path / "blocked-72h.db", allow_soak_24h=True)
    try:
        code, _, _ = server.response(
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body(soak_72h=True),
        )
        assert code == 400
        assert server._owner.snapshot() is None  # noqa: SLF001
    finally:
        server.close()
        store.close()
