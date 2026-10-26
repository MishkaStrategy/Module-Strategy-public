"""Canonical composite SIGNAL runtime for the owner-bound FVG-RT family.

The composite is deliberately thin: it delegates signal construction to the
three already-accepted historical-parity producers and only normalizes Core
emitter provenance. Child module/version/setup/signal identity stays immutable
inside each snapshot payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from modular_core import Feature, MarketView, ModuleDecision, ModuleManifest, StrategyModule

from .frozen import (
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    Frozen1hShortModule,
    Frozen4hShortModule,
)
from .module import FEATURE_KEY, FvgRtFinal15mModule

COMPOSITE_MODULE_NAME = "fvg_rt_composite"
COMPOSITE_MODULE_VERSION = "1.0.0"
COMPOSITE_CONTRACT = "canonical-fvg-rt-three-component-composite-v1"

_COMPONENT_ORDER = (
    "fvg_rt_final_15m",
    "frozen_1h_short",
    "frozen_4h_short",
)
_PRODUCED_FEATURES = (
    FEATURE_KEY,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
)
_REQUIRED_TIMEFRAMES = ("15m", "1h", "4h", "1d")


class FvgRtCompositeModule(StrategyModule):
    """Expose the accepted three-component FVG-RT composition as one SIGNAL."""

    name = COMPOSITE_MODULE_NAME

    def __init__(
        self,
        *,
        contract: str = COMPOSITE_CONTRACT,
        component_ids: Sequence[str] = _COMPONENT_ORDER,
    ) -> None:
        if contract != COMPOSITE_CONTRACT:
            raise ValueError("canonical FVG-RT composite contract drift")
        if isinstance(component_ids, (str, bytes)) or not isinstance(component_ids, Sequence):
            raise ValueError("canonical FVG-RT component IDs must be a sequence")
        normalized_component_ids = tuple(component_ids)
        if normalized_component_ids != _COMPONENT_ORDER:
            raise ValueError("canonical FVG-RT component identity drift")

        self._components: tuple[StrategyModule, ...] = (
            FvgRtFinal15mModule(),
            Frozen1hShortModule(),
            Frozen4hShortModule(),
        )
        if tuple(component.name for component in self._components) != _COMPONENT_ORDER:
            raise RuntimeError("canonical FVG-RT component order drift")
        if any(component.manifest is None for component in self._components):
            raise RuntimeError("canonical FVG-RT component manifest is unavailable")

        self.parameters: Mapping[str, object] = {
            "contract": contract,
            "component_ids": normalized_component_ids,
        }
        self.manifest = ModuleManifest(
            name=self.name,
            version=COMPOSITE_MODULE_VERSION,
            api_version="1.0",
            required_timeframes=_REQUIRED_TIMEFRAMES,
            produced_features=_PRODUCED_FEATURES,
            warmup_bars={"15m": 200, "1h": 200, "4h": 200, "1d": 200},
            parameters_schema={
                "contract": {"type": "string", "default": COMPOSITE_CONTRACT},
                "component_ids": {"type": "array", "default": _COMPONENT_ORDER},
            },
            skip_until_ready=False,
            description=(
                "Owner-bound composite SIGNAL runtime for FVG-RT Final 15m + "
                "Frozen 1H Short + Frozen 4H Short."
            ),
            state_schema_version="1",
        )

    def on_start(self) -> None:
        for component in self._components:
            component.on_start()

    def on_bar(self, view: MarketView) -> ModuleDecision:
        features: list[Feature] = []
        skips: list[str] = []
        diagnostics: dict[str, object] = {}

        for component in self._components:
            decision = component.on_bar(view)
            self._validate_component_decision(component, decision)
            features.extend(
                self._reemit_feature(component, feature) for feature in decision.features
            )
            skips.extend(decision.skips)
            diagnostics[component.name] = dict(decision.diagnostics)

        return ModuleDecision(
            features=tuple(features),
            skips=tuple(skips),
            diagnostics={
                "contract": COMPOSITE_CONTRACT,
                "component_diagnostics": diagnostics,
                "snapshots_published": len(features),
            },
        )

    def on_finish(self) -> None:
        for component in self._components:
            component.on_finish()

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "payload_version": 1,
            "components": {
                component.name: dict(component.snapshot_state()) for component in self._components
            },
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        if set(state) != {"payload_version", "components"}:
            raise ValueError("invalid composite FVG-RT state keys")
        version = state["payload_version"]
        if isinstance(version, bool) or version != 1:
            raise ValueError("unsupported composite FVG-RT state payload version")
        components = state["components"]
        if not isinstance(components, Mapping):
            raise ValueError("composite FVG-RT components state must be a mapping")
        if set(components) != set(_COMPONENT_ORDER):
            raise ValueError("composite FVG-RT component state identity drift")

        for component in self._components:
            component_state = components[component.name]
            if not isinstance(component_state, Mapping):
                raise ValueError(f"composite FVG-RT state for {component.name} must be a mapping")
            component.restore_state(component_state)

    @staticmethod
    def _validate_component_decision(
        component: StrategyModule,
        decision: ModuleDecision,
    ) -> None:
        if (
            decision.setups
            or decision.signals
            or decision.feature_deletes
            or decision.orders
            or decision.cancellations
            or decision.position_updates
            or decision.closes
        ):
            raise RuntimeError(
                f"canonical FVG-RT component {component.name} emitted unsupported side effects"
            )

    def _reemit_feature(self, component: StrategyModule, feature: Feature) -> Feature:
        manifest = component.manifest
        if manifest is None:
            raise RuntimeError(f"canonical FVG-RT component {component.name} lost its manifest")
        if feature.source_module != component.name:
            raise RuntimeError("canonical FVG-RT child feature emitter identity drift")
        if feature.key not in manifest.produced_features:
            raise RuntimeError("canonical FVG-RT child emitted an undeclared feature")
        if feature.key not in _PRODUCED_FEATURES:
            raise RuntimeError("canonical FVG-RT composite feature inventory drift")
        payload = feature.value
        if not isinstance(payload, Mapping):
            raise RuntimeError("canonical FVG-RT child snapshot must be a mapping")
        if payload.get("module_id") != component.name:
            raise RuntimeError("canonical FVG-RT child snapshot module identity drift")
        if payload.get("module_version") != manifest.version:
            raise RuntimeError("canonical FVG-RT child snapshot version identity drift")

        return Feature(
            key=feature.key,
            source_module=self.name,
            created_at=feature.created_at,
            value=payload,
            symbol=feature.symbol,
            timeframe=feature.timeframe,
            valid_until=feature.valid_until,
            schema_version=feature.schema_version,
            feature_id=feature.feature_id,
            invalidate_on_gap=feature.invalidate_on_gap,
            metadata=feature.metadata,
        )
