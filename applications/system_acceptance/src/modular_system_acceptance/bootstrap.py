from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import (
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceOwnerError,
    AcceptancePhase,
)
from .daemon import SystemAcceptanceDaemon
from .executors import CheckpointedSoakExecutor
from .owner import PhaseExecutionContext, SystemAcceptanceOwner
from .production_backtest import (
    BacktestDeterminismProof,
    BacktestRunProof,
    TrustedBacktestDeterminismAdapter,
    TrustedBacktestRecoveryAdapter,
    TrustedBacktestRunAdapter,
)
from .production_composition import TrustedProductionPhaseAdapters
from .production_dataset import RegisteredDatasetSelectionPort, TrustedDatasetPhaseAdapter
from .production_identity import TrustedAcceptanceIdentity
from .production_research import (
    PreparedResearchExperimentPort,
    ResearchRunnerPort,
    TrustedResearchPhaseAdapter,
)
from .production_runtime import RuntimeSmokeProof, TrustedRuntimeSmokeAdapter
from .production_verification import TrustedVerificationPhaseAdapter, VerificationProof
from .service import SystemAcceptanceService
from .storage import SQLiteAcceptanceStore
from .trusted_http import PinnedSystemAcceptanceHttpServer

_INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class SystemAcceptanceBootstrapConfig:
    """Server-owned process configuration for the durable acceptance service.

    Filesystem state and bearer tokens are intentionally excluded from repr so an
    operator/service manager can log the safe configuration identity without leaking
    deployment-private paths or credentials.
    """

    identity: TrustedAcceptanceIdentity
    state_path: Path = field(repr=False)
    instance_id: str = "system-acceptance-owner"
    reader_token: str = field(default="", repr=False)
    operator_token: str = field(default="", repr=False)
    port: int = 8781
    poll_seconds: float = 1.0
    lease_seconds: int = 30
    allow_soak_24h: bool = False
    operator_subject: str = "control-plane:local-operator"

    def __post_init__(self) -> None:
        if not isinstance(self.identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        if not isinstance(self.state_path, Path):
            raise TypeError("state_path must be Path")
        normalized_path = self.state_path.expanduser()
        if normalized_path.is_symlink():
            raise ValueError("state_path must not be a symlink")
        object.__setattr__(self, "state_path", normalized_path)
        if (
            not isinstance(self.instance_id, str)
            or _INSTANCE_ID.fullmatch(self.instance_id) is None
        ):
            raise ValueError("instance_id must be a portable owner identifier")
        _token(self.reader_token, "reader_token")
        _token(self.operator_token, "operator_token")
        if self.reader_token == self.operator_token:
            raise ValueError("reader_token and operator_token must be distinct")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("port must be between 0 and 65535")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, (int, float))
            or not 0.05 <= float(self.poll_seconds) <= 60.0
        ):
            raise ValueError("poll_seconds must be between 0.05 and 60")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 5 <= self.lease_seconds <= 300
        ):
            raise ValueError("lease_seconds must be between 5 and 300")
        if not isinstance(self.allow_soak_24h, bool):
            raise TypeError("allow_soak_24h must be bool")
        if (
            not isinstance(self.operator_subject, str)
            or _INSTANCE_ID.fullmatch(self.operator_subject) is None
        ):
            raise ValueError("operator_subject must be a portable identifier")


@dataclass(frozen=True, slots=True)
class SystemAcceptanceProductionBindings:
    """Deployment-owned typed inputs used to construct every production phase adapter."""

    environment_verify: Callable[[], VerificationProof] = field(repr=False)
    dataset_resolver: Callable[[], RegisteredDatasetSelectionPort] = field(repr=False)
    dataset_version: int
    dataset_timeframe: str
    backtest_a: Callable[[PhaseExecutionContext], BacktestRunProof] = field(repr=False)
    backtest_b: Callable[[PhaseExecutionContext], BacktestRunProof] = field(repr=False)
    determinism_compare: Callable[[PhaseExecutionContext], BacktestDeterminismProof] = field(
        repr=False
    )
    research_runner: ResearchRunnerPort = field(repr=False)
    research_prepared: PreparedResearchExperimentPort = field(repr=False)
    research_specification_fingerprint: str
    runtime_probe: Callable[[], RuntimeSmokeProof] = field(repr=False)
    recovery_run: Callable[[PhaseExecutionContext], BacktestRunProof] = field(repr=False)
    final_verify: Callable[[], VerificationProof] = field(repr=False)

    def build(self, identity: TrustedAcceptanceIdentity) -> TrustedProductionPhaseAdapters:
        if not isinstance(identity, TrustedAcceptanceIdentity):
            raise TypeError("identity must be TrustedAcceptanceIdentity")
        return TrustedProductionPhaseAdapters(
            environment=TrustedVerificationPhaseAdapter(
                AcceptancePhase.ENVIRONMENT,
                identity=identity,
                verify=self.environment_verify,
            ),
            dataset=TrustedDatasetPhaseAdapter(
                identity=identity,
                resolver=self.dataset_resolver,
                version=self.dataset_version,
                timeframe=self.dataset_timeframe,
            ),
            backtest_a=TrustedBacktestRunAdapter(
                AcceptancePhase.BACKTEST_A,
                identity=identity,
                run=self.backtest_a,
            ),
            backtest_b=TrustedBacktestRunAdapter(
                AcceptancePhase.BACKTEST_B,
                identity=identity,
                run=self.backtest_b,
            ),
            determinism=TrustedBacktestDeterminismAdapter(
                identity=identity,
                compare=self.determinism_compare,
            ),
            research=TrustedResearchPhaseAdapter(
                identity=identity,
                runner=self.research_runner,
                prepared=self.research_prepared,
                specification_fingerprint=self.research_specification_fingerprint,
            ),
            runtime_smoke=TrustedRuntimeSmokeAdapter(
                identity=identity,
                probe=self.runtime_probe,
            ),
            recovery=TrustedBacktestRecoveryAdapter(
                identity=identity,
                run_recovery=self.recovery_run,
            ),
            final_verification=TrustedVerificationPhaseAdapter(
                AcceptancePhase.FINAL_VERIFICATION,
                identity=identity,
                verify=self.final_verify,
            ),
        )


class StagedPinnedSystemAcceptanceHttpServer(PinnedSystemAcceptanceHttpServer):
    """Pinned transport for the staged 2h -> explicit 24h acceptance plan.

    SOAK_2H is part of the baseline owner phase graph. The 24h stage must be enabled
    by deployment configuration and 72h is fail-closed for this bootstrap.
    """

    def __init__(self, *args: object, allow_soak_24h: bool, **kwargs: object) -> None:
        if not isinstance(allow_soak_24h, bool):
            raise TypeError("allow_soak_24h must be bool")
        self._allow_soak_24h = allow_soak_24h
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def _request(self, mutation: AcceptanceMutation, body: bytes) -> AcceptanceMutationRequest:
        request = super()._request(mutation, body)  # noqa: SLF001 - trusted transport policy layer
        if mutation is AcceptanceMutation.START:
            spec = request.spec
            if spec is None:
                raise ValueError("START request must contain a spec")
            if spec.enable_soak_72h:
                raise ValueError("72h soak is disabled by staged acceptance policy")
            if spec.enable_soak_24h and not self._allow_soak_24h:
                raise ValueError("24h soak is not enabled by deployment policy")
        return request


class ProductionSystemAcceptanceProcess:
    """Bounded operator-native host for the accepted durable owner and transport."""

    def __init__(
        self,
        *,
        config: SystemAcceptanceBootstrapConfig,
        phases: TrustedProductionPhaseAdapters,
        soak: CheckpointedSoakExecutor,
        on_error: Callable[[AcceptanceOwnerError], None] | None = None,
    ) -> None:
        if not isinstance(config, SystemAcceptanceBootstrapConfig):
            raise TypeError("config must be SystemAcceptanceBootstrapConfig")
        if not isinstance(phases, TrustedProductionPhaseAdapters):
            raise TypeError("phases must be TrustedProductionPhaseAdapters")
        if not isinstance(soak, CheckpointedSoakExecutor):
            raise TypeError("soak must be CheckpointedSoakExecutor")

        store = SQLiteAcceptanceStore(config.state_path)
        try:
            registry = phases.build_registry(soak=soak)
            owner = SystemAcceptanceOwner(
                store,
                registry,
                instance_id=config.instance_id,
                lease_seconds=config.lease_seconds,
            )
            daemon = SystemAcceptanceDaemon(
                owner,
                poll_seconds=config.poll_seconds,
                on_error=on_error,
            )
            http = StagedPinnedSystemAcceptanceHttpServer(
                owner,
                full_rc_sha=config.identity.full_rc_sha,
                reader_token=config.reader_token,
                operator_token=config.operator_token,
                port=config.port,
                operator_subject=config.operator_subject,
                allow_soak_24h=config.allow_soak_24h,
            )
            service = SystemAcceptanceService(daemon, http)
        except BaseException:
            store.close()
            raise

        self.config = config
        self.owner = owner
        self._store = store
        self._http = http
        self._service = service
        self._closed = False

    def start(self) -> None:
        try:
            self._service.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._service.close()
        finally:
            self._store.close()

    @property
    def running(self) -> bool:
        return self._service.running

    @property
    def address(self) -> tuple[str, int]:
        return self._http.address

    def __enter__(self) -> ProductionSystemAcceptanceProcess:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def build_production_system_acceptance_process(
    *,
    config: SystemAcceptanceBootstrapConfig,
    bindings: SystemAcceptanceProductionBindings,
    soak: CheckpointedSoakExecutor,
    on_error: Callable[[AcceptanceOwnerError], None] | None = None,
) -> ProductionSystemAcceptanceProcess:
    """Construct the full production composition from deployment-owned typed bindings."""

    if not isinstance(bindings, SystemAcceptanceProductionBindings):
        raise TypeError("bindings must be SystemAcceptanceProductionBindings")
    phases = bindings.build(config.identity)
    return ProductionSystemAcceptanceProcess(
        config=config,
        phases=phases,
        soak=soak,
        on_error=on_error,
    )


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")


__all__ = [
    "ProductionSystemAcceptanceProcess",
    "StagedPinnedSystemAcceptanceHttpServer",
    "SystemAcceptanceBootstrapConfig",
    "SystemAcceptanceProductionBindings",
    "build_production_system_acceptance_process",
]
