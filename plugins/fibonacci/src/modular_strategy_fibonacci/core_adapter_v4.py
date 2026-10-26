"""Cycle-4 Core adapter preserving the audited v0.3 Core boundary."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from modular_core import Feature, MarketView, ModuleDecision

from .config import FibonacciConfig
from .core_adapter import (
    FibonacciCoreStateError,
    FibonacciModule as _CoreAuditedFibonacciModule,
)
from .core_contracts import SHORT_SETUP_KEY
from .corrections.short_setup import (
    active_bearish_continuation_setup,
    build_bearish_reaction_profile,
    build_bearish_reaction_regime_profile,
)

MODULE_VERSION = "0.4.0"
STATE_SCHEMA_VERSION = "2"


class FibonacciModule(_CoreAuditedFibonacciModule):
    """Regime-aware feature producer layered over the audited Core adapter."""

    def __init__(
        self,
        *,
        timeframes: Sequence[str] = ("15m", "1h", "4h", "1d"),
        dataset_hash: str = "core-stream-unhashed",
        min_atr_multiple: str | int | float | Decimal = "1.5",
        publish_short_setup: bool = True,
        short_reaction_lookback: int = 40,
        short_reaction_minimum_samples: int = 8,
        short_reaction_minimum_success_rate: str | int | float | Decimal = "0.65",
        short_reaction_minimum_streak: int = 3,
        short_regime_window_size: int = 12,
        short_regime_minimum_window_samples: int = 8,
        short_regime_minimum_success_rate: str | int | float | Decimal = "0.65",
        short_regime_minimum_level_samples: int = 4,
        short_regime_minimum_level_success_rate: str | int | float | Decimal = "0.65",
        short_regime_change_threshold: str | int | float | Decimal = "0.15",
    ) -> None:
        # The base adapter retains the exact audited Core protocol, checkpoint,
        # gap-barrier, and tail-only processing implementation from Draft PR #243.
        super().__init__(
            timeframes=timeframes,
            dataset_hash=dataset_hash,
            min_atr_multiple=min_atr_multiple,
            publish_short_setup=False,
        )
        self.publish_short_setup = bool(publish_short_setup)
        self.config = FibonacciConfig(
            min_atr_multiple=Decimal(str(min_atr_multiple)),
            short_reaction_lookback=short_reaction_lookback,
            short_reaction_minimum_samples=short_reaction_minimum_samples,
            short_reaction_minimum_success_rate=Decimal(str(short_reaction_minimum_success_rate)),
            short_reaction_minimum_streak=short_reaction_minimum_streak,
            short_regime_window_size=short_regime_window_size,
            short_regime_minimum_window_samples=short_regime_minimum_window_samples,
            short_regime_minimum_success_rate=Decimal(str(short_regime_minimum_success_rate)),
            short_regime_minimum_level_samples=short_regime_minimum_level_samples,
            short_regime_minimum_level_success_rate=Decimal(str(short_regime_minimum_level_success_rate)),
            short_regime_change_threshold=Decimal(str(short_regime_change_threshold)),
        )
        parameters: dict[str, object] = {
            "timeframes": list(self.timeframes),
            "dataset_hash": self.dataset_hash,
            "min_atr_multiple": str(self.config.min_atr_multiple),
            "publish_short_setup": self.publish_short_setup,
            "short_reaction_lookback": self.config.short_reaction_lookback,
            "short_reaction_minimum_samples": (self.config.short_reaction_minimum_samples),
            "short_reaction_minimum_success_rate": str(self.config.short_reaction_minimum_success_rate),
            "short_reaction_minimum_streak": self.config.short_reaction_minimum_streak,
            "short_regime_window_size": self.config.short_regime_window_size,
            "short_regime_minimum_window_samples": (self.config.short_regime_minimum_window_samples),
            "short_regime_minimum_success_rate": str(self.config.short_regime_minimum_success_rate),
            "short_regime_minimum_level_samples": (self.config.short_regime_minimum_level_samples),
            "short_regime_minimum_level_success_rate": str(self.config.short_regime_minimum_level_success_rate),
            "short_regime_change_threshold": str(self.config.short_regime_change_threshold),
        }
        self.parameters = MappingProxyType(parameters)
        manifest = self.manifest
        if manifest is None:
            raise FibonacciCoreStateError("Fibonacci Core manifest is unavailable")
        schema = dict(manifest.parameters_schema)
        schema.update(
            {
                "short_reaction_lookback": {"type": "integer", "default": 40},
                "short_reaction_minimum_samples": {"type": "integer", "default": 8},
                "short_reaction_minimum_success_rate": {"type": "string", "default": "0.65"},
                "short_reaction_minimum_streak": {"type": "integer", "default": 3},
                "short_regime_window_size": {"type": "integer", "default": 12},
                "short_regime_minimum_window_samples": {"type": "integer", "default": 8},
                "short_regime_minimum_success_rate": {"type": "string", "default": "0.65"},
                "short_regime_minimum_level_samples": {"type": "integer", "default": 4},
                "short_regime_minimum_level_success_rate": {"type": "string", "default": "0.65"},
                "short_regime_change_threshold": {"type": "string", "default": "0.15"},
            }
        )
        self.manifest = replace(
            manifest,
            version=MODULE_VERSION,
            parameters_schema=schema,
            state_schema_version=STATE_SCHEMA_VERSION,
            description=(
                "Tracks full-endpoint Fibonacci waves and publishes immutable "
                "projections plus regime- and level-aware 4h/1d bearish reactions."
            ),
        )
        self._config_fingerprint = self._fingerprint_parameters(self.parameters)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        republish_pending = self._republish_pending
        publish_short_setup = self.publish_short_setup
        self.publish_short_setup = False
        try:
            decision = super().on_bar(view)
        finally:
            self.publish_short_setup = publish_short_setup
        if not publish_short_setup or not view.base_bars or view.data_gap:
            return decision

        features = list(decision.features)
        deletes = list(decision.feature_deletes)
        diagnostics = dict(decision.diagnostics)
        symbol = view.current.symbol
        for timeframe in self.timeframes:
            if timeframe not in {"4h", "1d"}:
                continue
            key = (symbol, timeframe)
            engine = self._engines.get(key)
            if engine is None:
                continue
            series = self._series_for(view, timeframe)
            if not series:
                continue
            latest = series[-1]
            snapshot = engine.snapshot(latest.close_time)
            profile = build_bearish_reaction_profile(
                snapshot,
                minimum_samples=self.config.short_reaction_minimum_samples,
                minimum_success_rate=float(self.config.short_reaction_minimum_success_rate),
                minimum_streak=self.config.short_reaction_minimum_streak,
                lookback_observations=self.config.short_reaction_lookback,
            )
            regime = build_bearish_reaction_regime_profile(
                snapshot,
                window_size=self.config.short_regime_window_size,
                minimum_window_samples=self.config.short_regime_minimum_window_samples,
                minimum_success_rate=float(self.config.short_regime_minimum_success_rate),
                minimum_level_samples=self.config.short_regime_minimum_level_samples,
                minimum_level_success_rate=float(self.config.short_regime_minimum_level_success_rate),
                change_threshold=float(self.config.short_regime_change_threshold),
            )
            setup = active_bearish_continuation_setup(snapshot, profile, regime)
            previous_id = self._short_feature_ids.get(key)
            if setup is None:
                if previous_id is not None:
                    deletes.append(
                        self._delete(
                            SHORT_SETUP_KEY,
                            previous_id,
                            symbol,
                            timeframe,
                            "fibonacci_short_setup_inactive",
                        )
                    )
                    self._short_feature_ids.pop(key, None)
                continue
            if previous_id == setup.setup_id and not republish_pending:
                continue
            if previous_id is not None and previous_id != setup.setup_id:
                deletes.append(
                    self._delete(
                        SHORT_SETUP_KEY,
                        previous_id,
                        symbol,
                        timeframe,
                        "fibonacci_short_setup_revised",
                    )
                )
            features.append(
                Feature(
                    key=SHORT_SETUP_KEY,
                    source_module=self.name,
                    created_at=min(setup.armed_at, latest.close_time),
                    value={
                        "setup_id": setup.setup_id,
                        "wave_id": setup.wave_id,
                        "sequence_root_id": setup.sequence_root_id,
                        "endpoint_revision": setup.endpoint_revision,
                        "correction_level": str(setup.correction_level),
                        "correction_ratio": str(setup.correction_ratio),
                        "correction_price": str(setup.correction_price),
                        "impulse_start_price": str(setup.impulse_start_price),
                        "impulse_end_price": str(setup.impulse_end_price),
                        "historical_success_rate": setup.historical_success_rate,
                        "historical_sample_size": setup.historical_sample_size,
                        "current_success_streak": setup.current_success_streak,
                        "current_regime": setup.current_regime,
                        "recent_success_rate": setup.recent_success_rate,
                        "recent_sample_size": setup.recent_sample_size,
                        "prior_success_rate": setup.prior_success_rate,
                        "prior_sample_size": setup.prior_sample_size,
                        "recent_level_success_rate": setup.recent_level_success_rate,
                        "recent_level_sample_size": setup.recent_level_sample_size,
                        "regime_success_rate_delta": setup.regime_success_rate_delta,
                        "evidence": setup.evidence,
                    },
                    symbol=symbol,
                    timeframe=timeframe,
                    schema_version="2",
                    feature_id=setup.setup_id,
                    invalidate_on_gap=True,
                    metadata={
                        "execution": "none",
                        "kind": "analytical_short_setup",
                        "profile_schema": "bearish-reaction-regime/v1",
                    },
                )
            )
            self._short_feature_ids[key] = setup.setup_id
            self._increment_counter(diagnostics, "short_setups_published")

        return replace(
            decision,
            features=tuple(features),
            feature_deletes=tuple(deletes),
            diagnostics=MappingProxyType(diagnostics),
        )

    def snapshot_state(self) -> Mapping[str, object]:
        state = dict(super().snapshot_state())
        state["state_schema_version"] = STATE_SCHEMA_VERSION
        state["module_version"] = MODULE_VERSION
        return state

    def restore_state(self, state: Mapping[str, object]) -> None:
        if str(state.get("state_schema_version")) != STATE_SCHEMA_VERSION:
            raise FibonacciCoreStateError("unsupported Fibonacci Core state schema")
        if str(state.get("module_version")) != MODULE_VERSION:
            raise FibonacciCoreStateError("Fibonacci Core module version mismatch")
        adapted = dict(state)
        adapted["state_schema_version"] = "1"
        adapted["module_version"] = "0.3.0"
        super().restore_state(adapted)


__all__ = [
    "FibonacciCoreStateError",
    "FibonacciModule",
    "MODULE_VERSION",
    "STATE_SCHEMA_VERSION",
]
