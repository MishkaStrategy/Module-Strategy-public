from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, cast

from modular_core import FeatureView, MarketView

from .config import Numeric, ReferenceMode
from .models import BreakoutReturnCounters, BreakoutReturnStateError
from .module import (
    BreakoutReturnModule as _BreakoutReturnModuleV1,
    _BarValues,
    _ReferenceResolution,
)


class BreakoutReturnModule(_BreakoutReturnModuleV1):
    """Compatibility-preserving hardening layer for the v1 detector.

    The class inherits the audited v1 state machine and narrows only
    serialization, restore validation, identifier, diagnostics, and optional
    upstream-producer binding semantics. It does not duplicate trading logic or
    alter the public package import path.
    """

    _IN_FLIGHT_DIAGNOSTIC_EVENTS = frozenset(
        {
            "return_expired",
            "return_too_deep",
            "return_confirmed",
            "waiting_return",
        }
    )

    def __init__(
        self,
        *,
        reference_mode: str = "rolling",
        reference_feature_key: str = "range.current",
        reference_feature_source_module: str | None = None,
        reference_feature_timeframe: str | None = None,
        reference_feature_schema_version: str | None = None,
        max_reference_age_seconds: int | None = None,
        lookback_bars: int = 20,
        direction: str = "both",
        breakout_confirmation: str = "body",
        return_confirmation: str = "body",
        breakout_buffer_bps: Numeric = 0.0,
        min_return_depth: Numeric = 0.02,
        max_return_depth: Numeric = 0.20,
        max_return_bars: int = 36,
        range_change_policy: str = "invalidate",
    ) -> None:
        if reference_feature_source_module is not None and (
            not isinstance(reference_feature_source_module, str)
            or not reference_feature_source_module.strip()
            or reference_feature_source_module != reference_feature_source_module.strip()
        ):
            raise ValueError(
                "invalid breakout_return configuration: "
                "reference_feature_source_module must be a nonblank trimmed string or None"
            )
        self._reference_feature_source_module = reference_feature_source_module
        super().__init__(
            reference_mode=reference_mode,
            reference_feature_key=reference_feature_key,
            reference_feature_timeframe=reference_feature_timeframe,
            reference_feature_schema_version=reference_feature_schema_version,
            max_reference_age_seconds=max_reference_age_seconds,
            lookback_bars=lookback_bars,
            direction=direction,
            breakout_confirmation=breakout_confirmation,
            return_confirmation=return_confirmation,
            breakout_buffer_bps=breakout_buffer_bps,
            min_return_depth=min_return_depth,
            max_return_depth=max_return_depth,
            max_return_bars=max_return_bars,
            range_change_policy=range_change_policy,
        )
        if (
            reference_feature_source_module is not None
            and self.config.reference_mode is not ReferenceMode.FEATURE
        ):
            raise ValueError(
                "invalid breakout_return configuration: feature-only parameters require "
                "reference_mode='feature': reference_feature_source_module"
            )

        manifest = self.manifest
        if manifest is None:
            raise RuntimeError("breakout_return manifest was not initialized")
        parameter_schema = dict(manifest.parameters_schema)
        parameter_schema["reference_feature_source_module"] = {"type": "string"}
        self.manifest = replace(
            manifest,
            parameters_schema=MappingProxyType(parameter_schema),
        )
        if reference_feature_source_module is not None:
            parameters = dict(self.parameters)
            parameters["reference_feature_source_module"] = reference_feature_source_module
            self.parameters = MappingProxyType(parameters)

    @property
    def reference_feature_source_module(self) -> str | None:
        """Optional producer identity enforced for feature-mode range lookup."""

        return self._reference_feature_source_module

    def _build_config_fingerprint(self) -> str:
        base_fingerprint = super()._build_config_fingerprint()
        source_module = self._reference_feature_source_module
        if source_module is None:
            return base_fingerprint
        encoded = json.dumps(
            {
                "base_fingerprint": base_fingerprint,
                "reference_feature_source_module": source_module,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _feature_reference(
        self,
        view: MarketView,
        values: _BarValues,
    ) -> _ReferenceResolution:
        source_module = self._reference_feature_source_module
        if source_module is None or view.features is None:
            return super()._feature_reference(view, values)

        matching = view.features.get_all(
            self.config.reference_feature_key,
            symbol=values.symbol,
            timeframe=self.config.reference_feature_timeframe,
            source_module=source_module,
        )
        filtered_features = FeatureView(matching, view.features.timestamp)
        return super()._feature_reference(
            replace(view, features=filtered_features),
            values,
        )

    def _diagnostics(
        self,
        event: str,
        **values: object,
    ) -> Mapping[str, object]:
        if event in self._IN_FLIGHT_DIAGNOSTIC_EVENTS and "sequence" not in values:
            values["sequence"] = self._sequence + 1
        return super()._diagnostics(event, **values)

    @staticmethod
    def _canonical_decimal(value: Decimal) -> str:
        if value == 0:
            return "0"
        return format(value.normalize(), "f")

    @staticmethod
    def _optional_identifier(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("reference.range_id must be a nonblank trimmed string when provided")
        return value

    def snapshot_state(self) -> Mapping[str, object]:
        state = dict(super().snapshot_state())
        state["maxima"] = [
            [sequence, self._canonical_decimal(Decimal(str(value)))]
            for sequence, value in cast(
                list[list[object]],
                state["maxima"],
            )
        ]
        state["minima"] = [
            [sequence, self._canonical_decimal(Decimal(str(value)))]
            for sequence, value in cast(
                list[list[object]],
                state["minima"],
            )
        ]
        active_raw = state.get("active")
        if isinstance(active_raw, Mapping):
            active = dict(cast(Mapping[str, object], active_raw))
            reference_raw = active.get("reference")
            if isinstance(reference_raw, Mapping):
                reference = dict(cast(Mapping[str, object], reference_raw))
                reference["high"] = self._canonical_decimal(Decimal(str(reference["high"])))
                reference["low"] = self._canonical_decimal(Decimal(str(reference["low"])))
                active["reference"] = reference
            active["extreme"] = self._canonical_decimal(Decimal(str(active["extreme"])))
            state["active"] = active
        state["counters"] = dict(self._counters.as_checkpoint_mapping())
        return state

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise BreakoutReturnStateError("state must be a mapping")
        try:
            version = self._integer(
                state.get("payload_version", 0),
                "payload_version",
            )
            sequence = self._integer(
                state.get("sequence", 0),
                "sequence",
            )
        except (TypeError, ValueError) as exc:
            raise BreakoutReturnStateError(f"invalid breakout_return state: {exc}") from exc

        bound_symbol_raw = state.get("bound_symbol")
        bound_timeframe_raw = state.get("bound_timeframe")
        if (bound_symbol_raw is None) != (bound_timeframe_raw is None):
            raise BreakoutReturnStateError("stream binding fields must appear together")
        if sequence == 0 and bound_symbol_raw is not None:
            raise BreakoutReturnStateError("empty state cannot contain stream binding")
        if sequence > 0 and bound_symbol_raw is None:
            raise BreakoutReturnStateError("processed state requires stream binding")
        if bound_timeframe_raw is not None:
            bound_timeframe = self._string(
                bound_timeframe_raw,
                "bound_timeframe",
            )
            self._parse_close_delta(bound_timeframe)

        if version == 2:
            counters_raw = state.get("counters")
            if not isinstance(counters_raw, Mapping):
                raise BreakoutReturnStateError("counters must be a mapping")
            try:
                counters = BreakoutReturnCounters.from_mapping(
                    cast(Mapping[str, object], counters_raw)
                )
            except ValueError as exc:
                raise BreakoutReturnStateError(f"invalid counters: {exc}") from exc
            armed = counters.armed_above + counters.armed_below
            terminal = (
                counters.confirmed + counters.rejected + counters.expired + counters.invalidated
            )
            expected_active = 1 if state.get("active") is not None else 0
            if armed != terminal + expected_active:
                raise BreakoutReturnStateError(
                    "counter lifecycle totals contradict active setup state"
                )

        super().restore_state(state)
        if version == 1 and self._active is not None:
            # Payload v1 predates lifecycle counters. Reconstruct the minimum
            # causal arm count required by the in-flight setup so the first
            # emitted payload-v2 checkpoint remains self-restorable.
            if self._active.direction.value == "above":
                self._counters.armed_above = 1
            else:
                self._counters.armed_below = 1
