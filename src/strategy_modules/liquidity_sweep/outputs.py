from __future__ import annotations

import hashlib

from modular_core import Feature, SetupEvent, SetupStatus, Side, Signal

from .constants import FEATURE_SCHEMA_VERSION, LEVEL_FEATURE_KEY, SWEEP_FEATURE_KEY
from .models import BarSnapshot, LiquidityLevel, SweepDirection, SweepMeasurement, public_float


def build_sweep_outputs(
    module_name: str,
    level: LiquidityLevel,
    bar: BarSnapshot,
    measurement: SweepMeasurement,
    age: int,
    *,
    emit_signal: bool,
    signal_strength: float,
) -> tuple[Feature, tuple[SetupEvent, SetupEvent], Signal | None]:
    raw = "|".join((level.level_id, bar.timestamp.isoformat(), measurement.direction.value))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    event_id = f"liq-sweep-{digest}"
    metadata = {
        "event_id": event_id,
        "decision_time": bar.close_time.isoformat(),
        "level_id": level.level_id,
        "liquidity_side": level.side.value,
        "level_price": public_float(level.price),
        "close_price": public_float(bar.close),
        "pivot_time": level.pivot_time.isoformat(),
        "confirmed_at": level.confirmed_at.isoformat(),
        "bars_since_confirmation": age,
        **dict(measurement.as_metadata()),
    }
    feature = Feature(
        key=SWEEP_FEATURE_KEY,
        source_module=module_name,
        created_at=bar.close_time,
        valid_until=bar.close_time,
        value=metadata,
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        schema_version=FEATURE_SCHEMA_VERSION,
        feature_id=f"feat-{event_id}",
        invalidate_on_gap=True,
    )
    side = Side.SELL if measurement.direction is SweepDirection.BEARISH else Side.BUY
    created_setup = SetupEvent(
        module=module_name,
        setup_id=event_id,
        status=SetupStatus.CREATED,
        reason="liquidity_sweep_detected",
        metadata=metadata,
        symbol=bar.symbol,
        side=side,
    )
    confirmed_setup = SetupEvent(
        module=module_name,
        setup_id=event_id,
        status=SetupStatus.CONFIRMED,
        reason=f"{measurement.direction.value}_liquidity_sweep",
        metadata=metadata,
        symbol=bar.symbol,
        side=side,
    )
    signal = None
    if emit_signal:
        signal = Signal(
            module=module_name,
            side=side,
            reason=f"{measurement.direction.value}_liquidity_sweep",
            strength=signal_strength,
            metadata=metadata,
        )
    return feature, (created_setup, confirmed_setup), signal


def level_feature(module_name: str, level: LiquidityLevel) -> Feature:
    return Feature(
        key=LEVEL_FEATURE_KEY,
        source_module=module_name,
        created_at=level.confirmed_at,
        value={
            "level_id": level.level_id,
            "liquidity_side": level.side.value,
            "price": public_float(level.price),
            "pivot_time": level.pivot_time.isoformat(),
            "confirmed_at": level.confirmed_at.isoformat(),
        },
        symbol=level.symbol,
        timeframe=level.timeframe,
        schema_version=FEATURE_SCHEMA_VERSION,
        feature_id=level.feature_id,
        invalidate_on_gap=True,
    )
