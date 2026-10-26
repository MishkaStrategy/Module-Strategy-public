from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from modular_core import (
    Candle,
    DependencyPlan,
    Feature,
    FeatureDelete,
    FeatureStore,
    FeatureView,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from .errors import PipelineContractError
from .market_data import MarketEventEnvelope


class MarketViewFactory(Protocol):
    def build(
        self,
        *,
        base_bars: tuple[Candle, ...],
        index: int,
        data_gap: bool,
        features: FeatureView,
    ) -> MarketView: ...


@dataclass(frozen=True, slots=True)
class PipelineBatch:
    event_id: str
    module_names: tuple[str, ...]
    decisions: tuple[ModuleDecision, ...]
    data_gap: bool
    features_invalidated: int


class CoreMarketPipeline:
    def __init__(
        self,
        *,
        dependency_plan: DependencyPlan,
        view_factory: MarketViewFactory,
        max_history_bars: int,
        max_features: int = 10000,
    ) -> None:
        if not isinstance(dependency_plan, DependencyPlan):
            raise TypeError("dependency_plan must be modular_core.DependencyPlan")
        if isinstance(max_history_bars, bool) or not isinstance(max_history_bars, int):
            raise TypeError("max_history_bars must be int")
        if max_history_bars < 1:
            raise ValueError("max_history_bars must be positive")
        if not callable(getattr(view_factory, "build", None)):
            raise TypeError("view_factory must provide build()")
        self._modules = tuple(dependency_plan.modules)
        self._manifests = self._manifest_map(dependency_plan)
        self._view_factory = view_factory
        self._max_history_bars = max_history_bars
        self._features = FeatureStore(max_features=max_features)
        self._histories: dict[tuple[str, str], deque[Candle]] = {}
        self._indices: dict[tuple[str, str], int] = {}

    @property
    def feature_store(self) -> FeatureStore:
        return self._features

    def process(self, event: MarketEventEnvelope, *, data_gap: bool) -> PipelineBatch:
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        if not isinstance(data_gap, bool):
            raise TypeError("data_gap must be bool")
        stream = (event.candle.symbol, event.candle.timeframe)
        working_history = deque(
            self._histories.get(stream, ()),
            maxlen=self._max_history_bars,
        )
        working_history.append(event.candle)
        working_features = self._features.copy()
        invalidated = 0
        if data_gap:
            invalidated = len(working_features.invalidate_on_gap(symbol=event.candle.symbol))
        index = self._indices.get(stream, -1) + 1
        snapshots = self._capture_module_states()

        decisions: list[ModuleDecision] = []
        module_names: list[str] = []
        try:
            for module in self._modules:
                manifest = self._manifests[module.name]
                view = self._view_factory.build(
                    base_bars=tuple(working_history),
                    index=index,
                    data_gap=data_gap,
                    features=working_features.snapshot(event.candle.close_time),
                )
                if not isinstance(view, MarketView):
                    raise PipelineContractError(
                        "view_factory returned a non-MarketView value",
                        reason_code="invalid_market_view",
                    )
                if view.current != event.candle:
                    raise PipelineContractError(
                        "MarketView current candle does not match accepted event",
                        reason_code="market_view_identity_mismatch",
                    )
                decision = module.on_bar(view)
                if not isinstance(decision, ModuleDecision):
                    raise PipelineContractError(
                        f"module {module.name} returned a non-ModuleDecision value",
                        reason_code="invalid_module_decision",
                    )
                self._preflight_decision(module, manifest, decision, event.candle)
                self._apply_feature_changes(decision, working_features)
                decisions.append(decision)
                module_names.append(module.name)
        except Exception as exc:
            self._restore_module_states(snapshots, original_error=exc)
            raise

        self._histories[stream] = working_history
        self._indices[stream] = index
        self._features = working_features
        return PipelineBatch(
            event_id=event.event_id,
            module_names=tuple(module_names),
            decisions=tuple(decisions),
            data_gap=data_gap,
            features_invalidated=invalidated,
        )

    def _capture_module_states(
        self,
    ) -> tuple[tuple[StrategyModule, Mapping[str, object]], ...]:
        snapshots: list[tuple[StrategyModule, Mapping[str, object]]] = []
        for module in self._modules:
            state = module.snapshot_state()
            if not isinstance(state, Mapping):
                raise PipelineContractError(
                    f"module {module.name} snapshot_state returned a non-mapping",
                    reason_code="invalid_module_snapshot",
                )
            snapshots.append((module, state))
        return tuple(snapshots)

    @staticmethod
    def _restore_module_states(
        snapshots: tuple[tuple[StrategyModule, Mapping[str, object]], ...],
        *,
        original_error: Exception,
    ) -> None:
        rollback_errors: list[Exception] = []
        for module, state in reversed(snapshots):
            try:
                module.restore_state(state)
            except Exception as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            group = ExceptionGroup(
                "module state rollback failures",
                [original_error, *rollback_errors],
            )
            raise PipelineContractError(
                "pipeline failed and module state rollback was incomplete",
                reason_code="pipeline_rollback_failed",
            ) from group

    @staticmethod
    def _manifest_map(plan: DependencyPlan) -> Mapping[str, ModuleManifest]:
        manifests = {manifest.name: manifest for manifest in plan.manifests}
        module_names = tuple(module.name for module in plan.modules)
        if set(manifests) != set(module_names):
            raise PipelineContractError(
                "dependency plan manifest/module names do not match",
                reason_code="dependency_plan_mismatch",
            )
        return manifests

    @staticmethod
    def _preflight_decision(
        module: StrategyModule,
        manifest: ModuleManifest,
        decision: ModuleDecision,
        candle: Candle,
    ) -> None:
        if (
            decision.orders
            or decision.cancellations
            or decision.position_updates
            or decision.closes
        ):
            raise PipelineContractError(
                f"module {module.name} attempted to bypass Decision Router/Paper Execution",
                reason_code="direct_execution_intent_forbidden",
            )
        for feature in decision.features:
            CoreMarketPipeline._validate_feature(module, manifest, feature, candle)
        for deletion in decision.feature_deletes:
            CoreMarketPipeline._validate_delete(module, manifest, deletion)
        for signal in decision.signals:
            if signal.module != module.name:
                raise PipelineContractError(
                    "signal.module must match emitting module",
                    reason_code="signal_source_mismatch",
                )
        for setup in decision.setups:
            if setup.module != module.name:
                raise PipelineContractError(
                    "setup.module must match emitting module",
                    reason_code="setup_source_mismatch",
                )

    @staticmethod
    def _validate_feature(
        module: StrategyModule,
        manifest: ModuleManifest,
        feature: Feature,
        candle: Candle,
    ) -> None:
        if feature.source_module != module.name:
            raise PipelineContractError(
                "feature.source_module must match emitting module",
                reason_code="feature_source_mismatch",
            )
        if feature.key not in manifest.produced_features:
            raise PipelineContractError(
                f"module {module.name} emitted undeclared feature {feature.key}",
                reason_code="undeclared_feature",
            )
        if feature.created_at > candle.close_time:
            raise PipelineContractError(
                "feature cannot be created after the accepted candle close",
                reason_code="future_feature",
            )

    @staticmethod
    def _validate_delete(
        module: StrategyModule,
        manifest: ModuleManifest,
        deletion: FeatureDelete,
    ) -> None:
        if deletion.source_module != module.name:
            raise PipelineContractError(
                "feature delete source must match emitting module",
                reason_code="feature_delete_source_mismatch",
            )
        if deletion.key not in manifest.produced_features:
            raise PipelineContractError(
                f"module {module.name} deleted undeclared feature {deletion.key}",
                reason_code="undeclared_feature_delete",
            )

    @staticmethod
    def _apply_feature_changes(
        decision: ModuleDecision,
        features: FeatureStore,
    ) -> None:
        for feature in decision.features:
            features.publish(feature)
        for deletion in decision.feature_deletes:
            features.delete(deletion)
