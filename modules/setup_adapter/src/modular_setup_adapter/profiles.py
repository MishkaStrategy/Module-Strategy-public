from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from datetime import UTC, timedelta
from decimal import Decimal
from types import MappingProxyType

from modular_core import Feature, MarketView, Side

from .config import SourceSpec
from .contract import build_canonical_setup_payload, validate_canonical_setup_payload
from .enums import AdapterReason, SourceProfile
from .primitives import aware_datetime, decimal_text, decimal_value, primitive, required_text

ProfileAdapter = Callable[[Feature, MarketView], Mapping[str, object]]

_FVG_RT_FEATURE_KEY = "fvg_rt_final_15m.snapshot"
_FVG_RT_MODULE_NAME = "fvg_rt_final_15m"
_FVG_RT_MODULE_VERSION = "2.0.0"
_FVG_RT_COMPOSITE_MODULE_NAME = "fvg_rt_composite"
_FVG_RT_FEATURE_CONTRACT = "canonical-fvg-final-15m-feature-snapshot-v1"
_FVG_RT_CONDITIONS_BY_SETUP: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "long": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bullish_fvg",
                "close_above_ema50",
                "completed_4h_close_above_ema200",
                "ema50_rising_4",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "location_12h_min",
                "location_8h_min",
                "middle_body_atr_min",
                "middle_bullish",
                "rsi14_min",
                "session_allowed",
                "volume_ratio_min",
            }
        ),
        "short_a": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bearish_fvg",
                "completed_1d_close_below_ema200",
                "completed_4h_trend_down_3",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "middle_bearish",
                "middle_body_atr_min",
                "short_location_4h_max",
                "volume_ratio_min",
            }
        ),
        "short_b": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bearish_bos_48",
                "bearish_fvg",
                "close_strength_min",
                "completed_1d_close_below_ema200",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "middle_bearish",
                "middle_body_atr_min",
                "no_recent_bullish_fvg_12",
                "rsi14_max",
                "short_location_48h_min",
                "short_location_8h_min",
                "volume_ratio_min",
            }
        ),
    }
)
_FVG_RT_COMPLETED_TIMEFRAMES_BY_SETUP: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "long": ("5m", "15m", "4h"),
        "short_a": ("5m", "15m", "4h", "1d"),
        "short_b": ("5m", "15m", "1d"),
    }
)
_FVG_RT_EXECUTION_BY_SETUP: Mapping[str, tuple[Decimal, int, int, int, Decimal]] = MappingProxyType(
    {
        "long": (Decimal("4.5"), 36, 288, 48, Decimal("0.15")),
        "short_a": (Decimal("5"), 48, 288, 24, Decimal("0.35")),
        "short_b": (Decimal("6"), 48, 576, 60, Decimal("0.35")),
    }
)

_FVG_RT_SETUP_ORDER: Mapping[str, int] = MappingProxyType(
    {
        "long": 0,
        "short_a": 1,
        "short_b": 2,
    }
)


_FVG_RT_MAX_STOP_FRACTION_BY_SETUP: Mapping[str, Decimal] = MappingProxyType(
    {
        "long": Decimal("0.05"),
        "short_a": Decimal("0.015"),
        "short_b": Decimal("0.03"),
    }
)

_FROZEN_1H_FEATURE_KEY = "frozen_1h_short.snapshot"
_FROZEN_1H_MODULE_NAME = "frozen_1h_short"
_FROZEN_1H_MODULE_VERSION = "1.0.0"
_FROZEN_1H_FEATURE_CONTRACT = "canonical-frozen-1h-short-feature-snapshot-v1"
_FROZEN_1H_CONDITIONS = frozenset(
    {
        "all_inputs_completed",
        "atr_fraction_inclusive",
        "bearish_fvg",
        "close_below_ema50",
        "completed_4h_close_below_ema200",
        "ema50_falling_4",
        "fvg_size_atr_inclusive",
        "impulse_efficiency_min",
        "middle_body_atr_min",
        "middle_bearish",
        "short_location_8h_min",
        "volume_ratio_min",
    }
)
_FROZEN_1H_COMPLETED = ("5m", "1h", "4h")
_FROZEN_1H_EXECUTION = (Decimal("6"), 48, 144, 72, Decimal("0.5"))
_FROZEN_1H_MAX_STOP_FRACTION = Decimal("0.04")

_FROZEN_4H_FEATURE_KEY = "frozen_4h_short.snapshot"
_FROZEN_4H_MODULE_NAME = "frozen_4h_short"
_FROZEN_4H_MODULE_VERSION = "1.0.0"
_FROZEN_4H_FEATURE_CONTRACT = "canonical-frozen-4h-short-feature-snapshot-v1"
_FROZEN_4H_CONDITIONS = frozenset(
    {
        "all_inputs_completed",
        "atr_fraction_inclusive",
        "bearish_fvg",
        "fvg_size_atr_inclusive",
        "impulse_efficiency_min",
        "middle_body_atr_min",
        "middle_bearish",
        "rsi14_max",
        "short_location_32h_min",
        "volume_ratio_min",
    }
)
_FROZEN_4H_COMPLETED = ("5m", "4h")
_FROZEN_4H_EXECUTION = (Decimal("6"), 576, 576, 432, Decimal("0.35"))
_FROZEN_4H_MAX_STOP_FRACTION = Decimal("0.08")


def adapt_source(
    source: SourceSpec,
    feature: Feature,
    view: MarketView,
) -> Mapping[str, object]:
    if source.source_module is not None and feature.source_module != source.source_module:
        raise ValueError(
            f"source module mismatch: expected {source.source_module}, got {feature.source_module}"
        )
    adapter = _PROFILE_ADAPTERS.get(source.profile)
    if adapter is None:
        raise ValueError(f"unsupported source profile: {source.profile.value}")
    canonical = adapter(feature, view)
    validate_canonical_setup_payload(canonical, feature=feature, view=view)
    return canonical


def adapt_breakout_return_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    if feature.schema_version != "1":
        raise ValueError(f"unsupported source schema: {feature.schema_version}")
    value = feature.value
    if not isinstance(value, Mapping):
        raise ValueError("source payload must be a mapping")
    current = view.current
    setup_id = required_text(value.get("setup_id"), "setup_id")
    symbol = required_text(value.get("symbol") or feature.symbol, "symbol")
    if symbol != current.symbol or feature.symbol not in {current.symbol, "*"}:
        raise ValueError("symbol mismatch between source feature, payload and current stream")
    payload_timeframe = value.get("timeframe")
    if payload_timeframe is not None and str(payload_timeframe) != current.timeframe:
        raise ValueError("timeframe mismatch between source payload and current stream")
    if feature.timeframe is not None and feature.timeframe != current.timeframe:
        raise ValueError("timeframe mismatch between source feature and current stream")

    side_text = required_text(value.get("signal_side"), "signal_side").lower()
    try:
        side = Side(side_text)
    except ValueError as exc:
        raise ValueError("signal_side must be buy or sell") from exc
    confirmed_at = aware_datetime(value.get("confirmation_time"), "confirmation_time")
    if confirmed_at != feature.created_at or feature.created_at != current.close_time:
        raise ValueError(
            "feature causality violation: confirmation and publication must be same-bar"
        )
    confirmation = decimal_value(value.get("return_close"), "return_close")
    extreme = decimal_value(value.get("breakout_extreme"), "breakout_extreme")
    if side is Side.BUY and extreme >= confirmation:
        raise ValueError("invalid price geometry: buy extreme must be below confirmation")
    if side is Side.SELL and extreme <= confirmation:
        raise ValueError("invalid price geometry: sell extreme must be above confirmation")

    direction = value.get("breakout_direction")
    if direction is not None:
        direction_text = str(direction).strip().lower()
        expected = "above" if side is Side.SELL else "below"
        if direction_text != expected:
            raise ValueError("invalid price geometry: breakout direction contradicts signal side")

    metadata_fields = (
        "breakout_direction",
        "range_high",
        "range_low",
        "range_width",
        "reference_id",
        "reference_source",
        "reference_created_at",
        "breakout_time",
        "breakout_extreme",
        "return_open",
        "return_close",
        "return_depth",
        "bars_to_return",
        "feature_revision",
    )
    source_metadata = {
        key: primitive(value[key], f"source_payload.{key}")
        for key in metadata_fields
        if key in value
    }
    return build_canonical_setup_payload(
        setup_id=setup_id,
        symbol=symbol,
        side=side,
        confirmed_at=confirmed_at.astimezone(UTC),
        confirmation_price=confirmation,
        extreme_price=extreme,
        metadata={
            "adapter_profile": SourceProfile.BREAKOUT_RETURN_V1.value,
            "source_feature_key": feature.key,
            "source_module": feature.source_module,
            "source_feature_id": feature.feature_id,
            "source": source_metadata,
        },
    )


def adapt_canonical_setup_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    """Re-publish an already canonical family setup with authoritative provenance."""

    if feature.schema_version != "1":
        raise ValueError(f"unsupported source schema: {feature.schema_version}")
    value = feature.value
    if not isinstance(value, Mapping):
        raise ValueError("source payload must be a mapping")
    allowed = {
        "setup_id",
        "status",
        "symbol",
        "side",
        "confirmed_at",
        "confirmation_price",
        "entry_kind",
        "extreme_price",
        "entry_price",
        "trigger_price",
        "limit_price",
        "entry_cutoff_at",
        "metadata",
    }
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown canonical setup fields: {', '.join(unknown)}")
    if required_text(value.get("status"), "status").lower() != "confirmed":
        raise ValueError("canonical setup status must be confirmed")

    side_text = required_text(value.get("side"), "side").lower()
    try:
        side = Side(side_text)
    except ValueError as exc:
        raise ValueError("side must be buy or sell") from exc

    source_metadata = value.get("metadata", {})
    if not isinstance(source_metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    normalized_metadata = primitive(source_metadata, "source_payload.metadata")
    if not isinstance(normalized_metadata, Mapping):
        raise ValueError("metadata must be a mapping")

    entry_kind_raw = value.get("entry_kind")
    entry_kind = required_text(entry_kind_raw, "entry_kind") if entry_kind_raw is not None else None

    def optional_decimal(name: str) -> Decimal | None:
        raw = value.get(name)
        return None if raw is None else decimal_value(raw, name)

    cutoff_raw = value.get("entry_cutoff_at")
    cutoff = None if cutoff_raw is None else aware_datetime(cutoff_raw, "entry_cutoff_at")

    return build_canonical_setup_payload(
        setup_id=required_text(value.get("setup_id"), "setup_id"),
        symbol=required_text(value.get("symbol") or feature.symbol, "symbol"),
        side=side,
        confirmed_at=aware_datetime(value.get("confirmed_at"), "confirmed_at"),
        confirmation_price=decimal_value(value.get("confirmation_price"), "confirmation_price"),
        entry_kind=entry_kind,
        extreme_price=optional_decimal("extreme_price"),
        entry_price=optional_decimal("entry_price"),
        trigger_price=optional_decimal("trigger_price"),
        limit_price=optional_decimal("limit_price"),
        entry_cutoff_at=cutoff,
        metadata={
            **dict(normalized_metadata),
            "adapter_profile": SourceProfile.CANONICAL_SETUP_V1.value,
            "source_feature_key": feature.key,
            "source_module": feature.source_module,
            "source_feature_id": feature.feature_id,
        },
    )


def adapt_liquidity_sweep_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    """Map a deterministic liquidity-sweep event into canonical setup schema 1."""

    if feature.schema_version != "1":
        raise ValueError(f"unsupported source schema: {feature.schema_version}")
    value = feature.value
    if not isinstance(value, Mapping):
        raise ValueError("source payload must be a mapping")

    allowed = {
        "event_id",
        "decision_time",
        "level_id",
        "liquidity_side",
        "level_price",
        "close_price",
        "pivot_time",
        "confirmed_at",
        "bars_since_confirmation",
        "direction",
        "penetration_bps",
        "wick_to_body_ratio",
        "wick_fraction",
        "extreme_price",
    }
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown liquidity sweep fields: {', '.join(unknown)}")

    event_id = required_text(value.get("event_id"), "event_id")
    if feature.feature_id is not None and feature.feature_id != f"feat-{event_id}":
        raise ValueError("source feature identity does not match liquidity sweep event_id")

    current = view.current
    if feature.timeframe is not None and feature.timeframe != current.timeframe:
        raise ValueError("timeframe mismatch between liquidity sweep feature and current stream")

    decision_time = aware_datetime(value.get("decision_time"), "decision_time")
    if decision_time != feature.created_at or feature.created_at != current.close_time:
        raise ValueError(
            "feature causality violation: liquidity sweep decision and publication must be same-bar"
        )

    direction = required_text(value.get("direction"), "direction").lower()
    liquidity_side = required_text(value.get("liquidity_side"), "liquidity_side").lower()
    if direction == "bullish":
        side = Side.BUY
        expected_liquidity_side = "low"
    elif direction == "bearish":
        side = Side.SELL
        expected_liquidity_side = "high"
    else:
        raise ValueError("liquidity sweep direction must be bullish or bearish")
    if liquidity_side != expected_liquidity_side:
        raise ValueError("invalid price geometry: liquidity side contradicts sweep direction")

    required_text(value.get("level_id"), "level_id")
    pivot_time = aware_datetime(value.get("pivot_time"), "pivot_time")
    level_confirmed_at = aware_datetime(value.get("confirmed_at"), "confirmed_at")
    if pivot_time > level_confirmed_at or level_confirmed_at > decision_time:
        raise ValueError("feature causality violation: liquidity level chronology is inconsistent")

    bars_since_confirmation = value.get("bars_since_confirmation")
    if (
        isinstance(bars_since_confirmation, bool)
        or not isinstance(bars_since_confirmation, int)
        or bars_since_confirmation < 0
    ):
        raise ValueError("bars_since_confirmation must be a non-negative integer")

    penetration = decimal_value(value.get("penetration_bps"), "penetration_bps", positive=False)
    wick_ratio = decimal_value(
        value.get("wick_to_body_ratio"), "wick_to_body_ratio", positive=False
    )
    wick_fraction = decimal_value(value.get("wick_fraction"), "wick_fraction", positive=False)
    if penetration < 0 or wick_ratio < 0 or not Decimal("0") <= wick_fraction <= Decimal("1"):
        raise ValueError("liquidity sweep diagnostics are outside canonical bounds")

    confirmation = decimal_value(value.get("close_price"), "close_price")
    extreme = decimal_value(value.get("extreme_price"), "extreme_price")
    level = decimal_value(value.get("level_price"), "level_price")
    current_close = decimal_value(current.close, "current.close")
    current_extreme = decimal_value(
        current.low if side is Side.BUY else current.high,
        "current.extreme",
    )
    if confirmation != current_close or extreme != current_extreme:
        raise ValueError("invalid price geometry: liquidity sweep prices drift from current bar")
    if side is Side.BUY:
        if extreme >= confirmation or extreme >= level or confirmation < level:
            raise ValueError("invalid price geometry: bullish sweep geometry is inconsistent")
    else:
        if extreme <= confirmation or extreme <= level or confirmation > level:
            raise ValueError("invalid price geometry: bearish sweep geometry is inconsistent")

    source_metadata = {
        key: primitive(value[key], f"source_payload.{key}")
        for key in sorted(allowed - {"event_id", "decision_time", "close_price", "extreme_price"})
    }
    source_metadata["source_timeframe"] = feature.timeframe

    return build_canonical_setup_payload(
        setup_id=event_id,
        symbol=required_text(feature.symbol, "symbol"),
        side=side,
        confirmed_at=decision_time.astimezone(UTC),
        confirmation_price=confirmation,
        extreme_price=extreme,
        metadata={
            "strategy_family": "liquidity_sweep",
            "adapter_profile": SourceProfile.LIQUIDITY_SWEEP_V1.value,
            "source_feature_key": feature.key,
            "source_module": feature.source_module,
            "source_feature_id": feature.feature_id,
            "source": source_metadata,
        },
    )


def _fvg_rt_lifecycle_metadata(
    *,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    target_r: Decimal,
    entry_ttl_bars: int,
    max_hold_bars: int,
    no_progress_bars: int,
    no_progress_min_mfe_r: Decimal,
) -> Mapping[str, object]:
    risk_distance = entry - stop if side is Side.BUY else stop - entry
    if risk_distance <= 0:
        raise ValueError("invalid price geometry: canonical FVG-RT risk distance")

    # The immutable historical FVG-RT oracle derives target_price with binary64
    # arithmetic. Source snapshots serialize entry/stop/target-R with 17
    # significant digits, so converting those decimals back to float restores
    # the exact historical operands. Preserve that target price byte-for-byte
    # while retaining the authoritative target-R separately in metadata.
    entry_float = float(entry)
    stop_float = float(stop)
    target_r_float = float(target_r)
    if not all(math.isfinite(value) for value in (entry_float, stop_float, target_r_float)):
        raise ValueError("invalid price geometry: canonical FVG-RT target operands")
    binary64_risk = entry_float - stop_float if side is Side.BUY else stop_float - entry_float
    target_float = (
        entry_float + target_r_float * binary64_risk
        if side is Side.BUY
        else entry_float - target_r_float * binary64_risk
    )
    if not math.isfinite(target_float) or target_float <= 0:
        raise ValueError("invalid price geometry: canonical FVG-RT target price")
    target = Decimal(format(target_float, ".17g"))
    return {
        "invalidation_price": decimal_text(stop),
        "target_price": decimal_text(target),
        "target_r": decimal_text(target_r),
        "entry_ttl_bars": entry_ttl_bars,
        "max_holding_seconds": max_hold_bars * 5 * 60,
        "no_progress_rule": {
            "after_seconds": no_progress_bars * 5 * 60,
            "minimum_mfe_r": decimal_text(no_progress_min_mfe_r),
        },
    }


def adapt_fvg_rt_final_15m_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    """Validate the owner-bound FVG-RT snapshot before publishing a setup."""

    if feature.schema_version != "1":
        raise ValueError(f"unsupported source schema: {feature.schema_version}")
    if feature.key != _FVG_RT_FEATURE_KEY or feature.source_module not in {
        _FVG_RT_MODULE_NAME,
        _FVG_RT_COMPOSITE_MODULE_NAME,
    }:
        raise ValueError("source module mismatch: expected canonical FVG-RT snapshot")
    if feature.timeframe != "15m":
        raise ValueError("timeframe mismatch: canonical FVG-RT source must be 15m")
    value = feature.value
    if not isinstance(value, Mapping):
        raise ValueError("source payload must be a mapping")
    allowed = {
        "snapshot_id",
        "module_id",
        "module_version",
        "setup_id",
        "symbol",
        "side",
        "signal_available_at",
        "first_eligible_5m_open",
        "completed_timeframes",
        "conditions",
        "entry_price",
        "stop_price",
        "target_r",
        "ttl_bars_5m",
        "max_hold_bars_5m",
        "no_progress_bars_5m",
        "no_progress_min_mfe_r",
        "semantic_input_digest",
        "feature_contract",
    }
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown canonical FVG-RT fields: {', '.join(unknown)}")
    snapshot_id = required_text(value.get("snapshot_id"), "snapshot_id")
    identity = {
        "available_at": required_text(value.get("signal_available_at"), "signal_available_at"),
        "entry_price": required_text(value.get("entry_price"), "entry_price"),
        "module_id": required_text(value.get("module_id"), "module_id"),
        "module_version": required_text(value.get("module_version"), "module_version"),
        "setup_id": required_text(value.get("setup_id"), "setup_id"),
        "stop_price": required_text(value.get("stop_price"), "stop_price"),
        "symbol": required_text(value.get("symbol"), "symbol"),
    }
    if snapshot_id != _fvg_rt_digest(identity):
        raise ValueError("source feature identity does not match canonical FVG-RT snapshot")
    if feature.feature_id != f"fvg-rt-{snapshot_id}":
        raise ValueError("source feature identity does not match canonical FVG-RT snapshot")
    if required_text(value.get("module_id"), "module_id") != _FVG_RT_MODULE_NAME:
        raise ValueError("source module mismatch: canonical FVG-RT module_id")
    if required_text(value.get("module_version"), "module_version") != _FVG_RT_MODULE_VERSION:
        raise ValueError("unsupported source schema: canonical FVG-RT version mismatch")
    if required_text(value.get("feature_contract"), "feature_contract") != _FVG_RT_FEATURE_CONTRACT:
        raise ValueError("unsupported source schema: canonical FVG-RT feature contract mismatch")
    current = view.current
    symbol = required_text(value.get("symbol"), "symbol")
    if symbol != current.symbol or feature.symbol != current.symbol:
        raise ValueError("symbol mismatch between canonical FVG-RT snapshot and current stream")
    available_at = aware_datetime(value.get("signal_available_at"), "signal_available_at")
    first_open = aware_datetime(value.get("first_eligible_5m_open"), "first_eligible_5m_open")
    if (
        available_at != first_open
        or available_at != feature.created_at
        or feature.created_at != current.close_time
    ):
        raise ValueError("feature causality violation: canonical FVG-RT availability drift")
    setup_id = required_text(value.get("setup_id"), "setup_id")
    try:
        expected_conditions = _FVG_RT_CONDITIONS_BY_SETUP[setup_id]
        expected_completed = _FVG_RT_COMPLETED_TIMEFRAMES_BY_SETUP[setup_id]
        expected_execution = _FVG_RT_EXECUTION_BY_SETUP[setup_id]
    except KeyError as exc:
        raise ValueError("invalid source payload: unknown canonical FVG-RT setup") from exc
    completed = value.get("completed_timeframes")
    if not isinstance(completed, (list, tuple)) or tuple(completed) != expected_completed:
        raise ValueError("timeframe mismatch: canonical FVG-RT completed timeframe identity")
    side_text = required_text(value.get("side"), "side").lower()
    expected_side = "buy" if setup_id == "long" else "sell"
    if side_text != expected_side:
        raise ValueError("invalid price geometry: canonical FVG-RT setup side mismatch")
    conditions = value.get("conditions")
    if not isinstance(conditions, Mapping) or set(conditions) != set(expected_conditions):
        raise ValueError("invalid source payload: canonical FVG-RT condition identity")
    if any(item is not True for item in conditions.values()):
        raise ValueError("invalid source payload: canonical FVG-RT failed condition")
    entry = decimal_value(value.get("entry_price"), "entry_price")
    stop = decimal_value(value.get("stop_price"), "stop_price")
    expected_semantic_digest = _fvg_rt_digest(
        {
            "available_at": available_at.astimezone(UTC).isoformat(),
            "entry": round(float(entry), 12),
            "module_id": _FVG_RT_MODULE_NAME,
            "module_version": _FVG_RT_MODULE_VERSION,
            "setup_id": setup_id,
            "stop": round(float(stop), 12),
        }
    )
    if (
        required_text(value.get("semantic_input_digest"), "semantic_input_digest")
        != expected_semantic_digest
    ):
        raise ValueError("invalid source payload: canonical FVG-RT semantic digest")
    confirmation = decimal_value(current.close, "current.close")
    if (side_text == "buy" and not stop < entry <= confirmation) or (
        side_text == "sell" and not stop > entry >= confirmation
    ):
        raise ValueError("invalid price geometry: canonical FVG-RT limit/stop geometry")
    stop_distance = entry - stop if side_text == "buy" else stop - entry
    if stop_distance / entry > _FVG_RT_MAX_STOP_FRACTION_BY_SETUP[setup_id]:
        raise ValueError("invalid price geometry: canonical FVG-RT stop distance drift")
    target_r = decimal_value(value.get("target_r"), "target_r")
    ttl_bars = value.get("ttl_bars_5m")
    max_hold = value.get("max_hold_bars_5m")
    no_progress = value.get("no_progress_bars_5m")
    minimum_mfe = decimal_value(value.get("no_progress_min_mfe_r"), "no_progress_min_mfe_r")
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in (ttl_bars, max_hold, no_progress)
    ):
        raise ValueError("invalid source payload: canonical FVG-RT execution window type")
    expected_target, expected_ttl, expected_hold, expected_progress, expected_mfe = (
        expected_execution
    )
    if (
        target_r != expected_target
        or ttl_bars != expected_ttl
        or max_hold != expected_hold
        or no_progress != expected_progress
        or float(minimum_mfe) != float(expected_mfe)
    ):
        raise ValueError("invalid source payload: canonical FVG-RT execution contract drift")
    required_text(value.get("semantic_input_digest"), "semantic_input_digest")
    side = Side(side_text)
    return build_canonical_setup_payload(
        setup_id=snapshot_id,
        symbol=symbol,
        side=side,
        confirmed_at=available_at.astimezone(UTC),
        confirmation_price=confirmation,
        entry_kind="limit_explicit",
        entry_price=entry,
        entry_cutoff_at=available_at.astimezone(UTC) + timedelta(minutes=5 * ttl_bars),
        metadata={
            "strategy_family": "fvg_rt",
            "strategy_id": "fvg_rt",
            "module_id": _FVG_RT_MODULE_NAME,
            "priority": -(10 + _FVG_RT_SETUP_ORDER[setup_id]),
            "historical_priority": 1,
            "historical_setup_priority": _FVG_RT_SETUP_ORDER[setup_id],
            **_fvg_rt_lifecycle_metadata(
                side=side,
                entry=entry,
                stop=stop,
                target_r=target_r,
                entry_ttl_bars=ttl_bars,
                max_hold_bars=max_hold,
                no_progress_bars=no_progress,
                no_progress_min_mfe_r=minimum_mfe,
            ),
            "adapter_profile": SourceProfile.FVG_RT_FINAL_15M_V1.value,
            "source_feature_key": feature.key,
            "source_module": feature.source_module,
            "source_feature_id": feature.feature_id,
            "source": primitive(value, "source_payload"),
        },
    )


def _adapt_fvg_rt_frozen_v1(
    feature: Feature,
    view: MarketView,
    *,
    feature_key: str,
    module_name: str,
    module_version: str,
    source_timeframe: str,
    feature_contract: str,
    expected_conditions: frozenset[str],
    expected_completed: tuple[str, ...],
    expected_execution: tuple[Decimal, int, int, int, Decimal],
    maximum_stop_fraction: Decimal,
    historical_priority: int,
    profile: SourceProfile,
) -> Mapping[str, object]:
    """Validate one immutable Frozen FVG-RT short snapshot."""

    if feature.schema_version != "1":
        raise ValueError(f"unsupported source schema: {feature.schema_version}")
    if feature.key != feature_key or feature.source_module not in {
        module_name,
        _FVG_RT_COMPOSITE_MODULE_NAME,
    }:
        raise ValueError("source module mismatch: expected canonical frozen FVG-RT snapshot")
    if feature.timeframe != source_timeframe:
        raise ValueError("timeframe mismatch: canonical frozen FVG-RT source timeframe")
    value = feature.value
    if not isinstance(value, Mapping):
        raise ValueError("source payload must be a mapping")
    allowed = {
        "snapshot_id",
        "module_id",
        "module_version",
        "setup_id",
        "symbol",
        "side",
        "signal_available_at",
        "first_eligible_5m_open",
        "completed_timeframes",
        "conditions",
        "entry_price",
        "stop_price",
        "target_r",
        "ttl_bars_5m",
        "max_hold_bars_5m",
        "no_progress_bars_5m",
        "no_progress_min_mfe_r",
        "semantic_input_digest",
        "feature_contract",
    }
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown canonical frozen FVG-RT fields: {', '.join(unknown)}")

    snapshot_id = required_text(value.get("snapshot_id"), "snapshot_id")
    identity = {
        "available_at": required_text(value.get("signal_available_at"), "signal_available_at"),
        "entry_price": required_text(value.get("entry_price"), "entry_price"),
        "module_id": required_text(value.get("module_id"), "module_id"),
        "module_version": required_text(value.get("module_version"), "module_version"),
        "setup_id": required_text(value.get("setup_id"), "setup_id"),
        "stop_price": required_text(value.get("stop_price"), "stop_price"),
        "symbol": required_text(value.get("symbol"), "symbol"),
    }
    if snapshot_id != _fvg_rt_digest(identity) or feature.feature_id != f"fvg-rt-{snapshot_id}":
        raise ValueError("source feature identity does not match canonical frozen FVG-RT snapshot")
    if required_text(value.get("module_id"), "module_id") != module_name:
        raise ValueError("source module mismatch: canonical frozen FVG-RT module_id")
    if required_text(value.get("module_version"), "module_version") != module_version:
        raise ValueError("unsupported source schema: canonical frozen FVG-RT version mismatch")
    if required_text(value.get("feature_contract"), "feature_contract") != feature_contract:
        raise ValueError(
            "unsupported source schema: canonical frozen FVG-RT feature contract mismatch"
        )

    current = view.current
    symbol = required_text(value.get("symbol"), "symbol")
    if symbol != current.symbol or feature.symbol != current.symbol:
        raise ValueError("symbol mismatch between canonical frozen FVG-RT snapshot and stream")
    available_at = aware_datetime(value.get("signal_available_at"), "signal_available_at")
    first_open = aware_datetime(value.get("first_eligible_5m_open"), "first_eligible_5m_open")
    if (
        available_at != first_open
        or available_at != feature.created_at
        or feature.created_at != current.close_time
    ):
        raise ValueError("feature causality violation: canonical frozen FVG-RT availability drift")

    if required_text(value.get("setup_id"), "setup_id") != "short":
        raise ValueError("invalid source payload: canonical frozen FVG-RT setup_id")
    if required_text(value.get("side"), "side").lower() != "sell":
        raise ValueError("invalid price geometry: canonical frozen FVG-RT side mismatch")
    completed = value.get("completed_timeframes")
    if not isinstance(completed, (list, tuple)) or tuple(completed) != expected_completed:
        raise ValueError("timeframe mismatch: canonical frozen FVG-RT completed timeframe identity")
    conditions = value.get("conditions")
    if not isinstance(conditions, Mapping) or set(conditions) != set(expected_conditions):
        raise ValueError("invalid source payload: canonical frozen FVG-RT condition identity")
    if any(item is not True for item in conditions.values()):
        raise ValueError("invalid source payload: canonical frozen FVG-RT failed condition")

    entry = decimal_value(value.get("entry_price"), "entry_price")
    stop = decimal_value(value.get("stop_price"), "stop_price")
    confirmation = decimal_value(current.close, "current.close")
    if not stop > entry >= confirmation:
        raise ValueError("invalid price geometry: canonical frozen FVG-RT limit/stop geometry")
    if (stop - entry) / entry > maximum_stop_fraction:
        raise ValueError("invalid price geometry: canonical frozen FVG-RT stop distance drift")

    expected_semantic_digest = _fvg_rt_digest(
        {
            "available_at": available_at.astimezone(UTC).isoformat(),
            "entry": round(float(entry), 12),
            "module_id": module_name,
            "module_version": module_version,
            "setup_id": "short",
            "stop": round(float(stop), 12),
        }
    )
    if (
        required_text(value.get("semantic_input_digest"), "semantic_input_digest")
        != expected_semantic_digest
    ):
        raise ValueError("invalid source payload: canonical frozen FVG-RT semantic digest")

    target_r = decimal_value(value.get("target_r"), "target_r")
    ttl_bars = value.get("ttl_bars_5m")
    max_hold = value.get("max_hold_bars_5m")
    no_progress = value.get("no_progress_bars_5m")
    minimum_mfe = decimal_value(value.get("no_progress_min_mfe_r"), "no_progress_min_mfe_r")
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in (ttl_bars, max_hold, no_progress)
    ):
        raise ValueError("invalid source payload: canonical frozen FVG-RT execution window type")
    expected_target, expected_ttl, expected_hold, expected_progress, expected_mfe = (
        expected_execution
    )
    if (
        target_r != expected_target
        or ttl_bars != expected_ttl
        or max_hold != expected_hold
        or no_progress != expected_progress
        or float(minimum_mfe) != float(expected_mfe)
    ):
        raise ValueError("invalid source payload: canonical frozen FVG-RT execution contract drift")

    return build_canonical_setup_payload(
        setup_id=snapshot_id,
        symbol=symbol,
        side=Side.SELL,
        confirmed_at=available_at.astimezone(UTC),
        confirmation_price=confirmation,
        entry_kind="limit_explicit",
        entry_price=entry,
        entry_cutoff_at=available_at.astimezone(UTC) + timedelta(minutes=5 * ttl_bars),
        metadata={
            "strategy_family": "fvg_rt",
            "strategy_id": "fvg_rt",
            "module_id": module_name,
            "priority": -(historical_priority * 10),
            "historical_priority": historical_priority,
            "historical_setup_priority": 0,
            **_fvg_rt_lifecycle_metadata(
                side=Side.SELL,
                entry=entry,
                stop=stop,
                target_r=target_r,
                entry_ttl_bars=ttl_bars,
                max_hold_bars=max_hold,
                no_progress_bars=no_progress,
                no_progress_min_mfe_r=minimum_mfe,
            ),
            "adapter_profile": profile.value,
            "source_feature_key": feature.key,
            "source_module": feature.source_module,
            "source_feature_id": feature.feature_id,
            "source": primitive(value, "source_payload"),
        },
    )


def adapt_fvg_rt_frozen_1h_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    return _adapt_fvg_rt_frozen_v1(
        feature,
        view,
        feature_key=_FROZEN_1H_FEATURE_KEY,
        module_name=_FROZEN_1H_MODULE_NAME,
        module_version=_FROZEN_1H_MODULE_VERSION,
        source_timeframe="1h",
        feature_contract=_FROZEN_1H_FEATURE_CONTRACT,
        expected_conditions=_FROZEN_1H_CONDITIONS,
        expected_completed=_FROZEN_1H_COMPLETED,
        expected_execution=_FROZEN_1H_EXECUTION,
        maximum_stop_fraction=_FROZEN_1H_MAX_STOP_FRACTION,
        historical_priority=2,
        profile=SourceProfile.FVG_RT_FROZEN_1H_V1,
    )


def adapt_fvg_rt_frozen_4h_v1(feature: Feature, view: MarketView) -> Mapping[str, object]:
    return _adapt_fvg_rt_frozen_v1(
        feature,
        view,
        feature_key=_FROZEN_4H_FEATURE_KEY,
        module_name=_FROZEN_4H_MODULE_NAME,
        module_version=_FROZEN_4H_MODULE_VERSION,
        source_timeframe="4h",
        feature_contract=_FROZEN_4H_FEATURE_CONTRACT,
        expected_conditions=_FROZEN_4H_CONDITIONS,
        expected_completed=_FROZEN_4H_COMPLETED,
        expected_execution=_FROZEN_4H_EXECUTION,
        maximum_stop_fraction=_FROZEN_4H_MAX_STOP_FRACTION,
        historical_priority=3,
        profile=SourceProfile.FVG_RT_FROZEN_4H_V1,
    )


def _fvg_rt_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


_PROFILE_ADAPTERS: Mapping[SourceProfile, ProfileAdapter] = MappingProxyType(
    {
        SourceProfile.BREAKOUT_RETURN_V1: adapt_breakout_return_v1,
        SourceProfile.CANONICAL_SETUP_V1: adapt_canonical_setup_v1,
        SourceProfile.FVG_RT_FINAL_15M_V1: adapt_fvg_rt_final_15m_v1,
        SourceProfile.FVG_RT_FROZEN_1H_V1: adapt_fvg_rt_frozen_1h_v1,
        SourceProfile.FVG_RT_FROZEN_4H_V1: adapt_fvg_rt_frozen_4h_v1,
        SourceProfile.LIQUIDITY_SWEEP_V1: adapt_liquidity_sweep_v1,
    }
)


def reason_for_error(message: str) -> AdapterReason:
    lowered = message.lower()
    if "unsupported source schema" in lowered:
        return AdapterReason.UNSUPPORTED_SOURCE_SCHEMA
    if "symbol mismatch" in lowered or "source module mismatch" in lowered:
        return AdapterReason.SYMBOL_MISMATCH
    if "timeframe mismatch" in lowered:
        return AdapterReason.TIMEFRAME_MISMATCH
    if "causality violation" in lowered:
        return AdapterReason.FEATURE_CAUSALITY_VIOLATION
    if "price geometry" in lowered:
        return AdapterReason.INVALID_PRICE_GEOMETRY
    return AdapterReason.INVALID_SOURCE_PAYLOAD
