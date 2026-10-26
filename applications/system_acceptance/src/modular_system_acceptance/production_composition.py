from __future__ import annotations

from dataclasses import dataclass

from .composition import build_complete_acceptance_registry
from .contracts import AcceptancePhase
from .executors import CheckpointedSoakExecutor
from .owner import AcceptanceExecutorRegistry
from .production_backtest import (
    TrustedBacktestDeterminismAdapter,
    TrustedBacktestRecoveryAdapter,
    TrustedBacktestRunAdapter,
)
from .production_dataset import TrustedDatasetPhaseAdapter
from .production_identity import TrustedAcceptanceIdentity
from .production_research import TrustedResearchPhaseAdapter
from .production_runtime import TrustedRuntimeSmokeAdapter
from .production_verification import TrustedVerificationPhaseAdapter


@dataclass(frozen=True, slots=True)
class TrustedProductionPhaseAdapters:
    """Typed deployment composition for every required non-soak acceptance phase."""

    environment: TrustedVerificationPhaseAdapter
    dataset: TrustedDatasetPhaseAdapter
    backtest_a: TrustedBacktestRunAdapter
    backtest_b: TrustedBacktestRunAdapter
    determinism: TrustedBacktestDeterminismAdapter
    research: TrustedResearchPhaseAdapter
    runtime_smoke: TrustedRuntimeSmokeAdapter
    recovery: TrustedBacktestRecoveryAdapter
    final_verification: TrustedVerificationPhaseAdapter

    def __post_init__(self) -> None:
        expected = (
            (self.environment, TrustedVerificationPhaseAdapter, AcceptancePhase.ENVIRONMENT),
            (self.dataset, TrustedDatasetPhaseAdapter, AcceptancePhase.DATASET),
            (self.backtest_a, TrustedBacktestRunAdapter, AcceptancePhase.BACKTEST_A),
            (self.backtest_b, TrustedBacktestRunAdapter, AcceptancePhase.BACKTEST_B),
            (
                self.determinism,
                TrustedBacktestDeterminismAdapter,
                AcceptancePhase.DETERMINISM,
            ),
            (self.research, TrustedResearchPhaseAdapter, AcceptancePhase.RESEARCH),
            (self.runtime_smoke, TrustedRuntimeSmokeAdapter, AcceptancePhase.RUNTIME_SMOKE),
            (self.recovery, TrustedBacktestRecoveryAdapter, AcceptancePhase.RECOVERY),
            (
                self.final_verification,
                TrustedVerificationPhaseAdapter,
                AcceptancePhase.FINAL_VERIFICATION,
            ),
        )
        for adapter, expected_type, phase in expected:
            if not isinstance(adapter, expected_type):
                raise TypeError(f"{phase.value} adapter has the wrong production type")
            executor = adapter.executor
            if executor.phase is not phase:
                raise ValueError(f"{phase.value} adapter is bound to the wrong phase")

        _require_shared_identity(tuple(adapter for adapter, _expected_type, _phase in expected))

    def build_registry(self, *, soak: CheckpointedSoakExecutor) -> AcceptanceExecutorRegistry:
        _require_shared_identity(
            (
                self.environment,
                self.dataset,
                self.backtest_a,
                self.backtest_b,
                self.determinism,
                self.research,
                self.runtime_smoke,
                self.recovery,
                self.final_verification,
            )
        )
        return build_complete_acceptance_registry(
            {
                AcceptancePhase.ENVIRONMENT: self.environment.executor,
                AcceptancePhase.DATASET: self.dataset.executor,
                AcceptancePhase.BACKTEST_A: self.backtest_a.executor,
                AcceptancePhase.BACKTEST_B: self.backtest_b.executor,
                AcceptancePhase.DETERMINISM: self.determinism.executor,
                AcceptancePhase.RESEARCH: self.research.executor,
                AcceptancePhase.RUNTIME_SMOKE: self.runtime_smoke.executor,
                AcceptancePhase.RECOVERY: self.recovery.executor,
                AcceptancePhase.FINAL_VERIFICATION: self.final_verification.executor,
            },
            soak=soak,
        )


def _require_shared_identity(adapters: tuple[object, ...]) -> None:
    identities = tuple(getattr(adapter, "_identity", None) for adapter in adapters)
    if any(not isinstance(identity, TrustedAcceptanceIdentity) for identity in identities):
        raise TypeError("every production phase adapter must expose a trusted identity")
    if any(identity != identities[0] for identity in identities[1:]):
        raise ValueError("production phase adapters must share one acceptance identity")


__all__ = ["TrustedProductionPhaseAdapters"]
