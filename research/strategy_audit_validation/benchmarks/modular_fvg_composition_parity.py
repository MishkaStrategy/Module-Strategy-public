"""Full-corpus parity gate for the owner-bound modular FVG-RT composition.

The immutable historical signal corpus remains the independent signal-detection
oracle.  This benchmark feeds every accepted historical signal through the real
runtime producer snapshot contract and canonical downstream adapters, verifies
same-bar Router arbitration, then delegates fills, fees, sizing economics,
slippage, lifecycle exits and restart behavior to the existing authoritative
Core/Backtest parity bridge.

It intentionally does not duplicate the historical signal-detection formulas.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pandas as pd
from modular_core import Candle, Feature, FeatureView, MarketView
from modular_core.series import ReadOnlySeries
from modular_core.timeframes import TimeframeSpec, TimeframeStore
from modular_modules.entry_model import EntryKind, EntryModelModule
from modular_setup_adapter import CanonicalSetupAdapterModule
from modular_strategy_protection_target import ProtectionTargetModule
from modular_strategy_risk_request_composer import RiskRequestComposerModule
from modular_strategy_trade_plan_router import TradePlanRouterModule
from strategy_modules.fvg_rt import (
    FEATURE_KEY,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    Frozen1hShortModule,
    Frozen4hShortModule,
    FvgRtFinal15mModule,
)
from strategy_modules.risk_sizing import RiskSizingModule

from ._common import (
    CANDIDATE_ID,
    COMPOSITION_HASH,
    INITIAL_EQUITY,
    Signal,
    load_5m,
)
from ._signals import build_signals
from .authoritative_historical_parity import (
    REFERENCE_SEMANTIC_DIGEST,
    _assert_core_fill_contract,
    _assert_ledger_exact,
    _assert_metrics_exact,
    _assert_order_contract,
    _assert_reference_identity,
    _authoritative_ledger,
    _exact_float,
    _load_baseline,
    _plan_from_signal,
    _run_one,
    _select_signal,
    _write_module_config,
    register_signal_plan,
    unregister_signal_plan,
)
from .canonical_strategy_v2 import _load_provenance

_ENTRY_KEY = "entry_model.plan"
_PROTECTION_KEY = "protection_target.plan"
_RISK_REQUEST_KEY = "risk.request"
_RISK_DECISION_KEY = "risk.decision"
_EXECUTABLE_PLAN_KEY = "trade_plan_router.executable_plan"
_FIVE_MINUTES = timedelta(minutes=5)
_SETUP_ORDER = {"long": 0, "short_a": 1, "short_b": 2, "short": 0}
_COMPONENT_PRIORITY = {
    "fvg_rt_final_15m": 1,
    "frozen_1h_short": 2,
    "frozen_4h_short": 3,
}
_SOURCE_TIMEFRAME = {
    "fvg_rt_final_15m": "15m",
    "frozen_1h_short": "1h",
    "frozen_4h_short": "4h",
}


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be a mapping")
    return cast(Mapping[str, Any], value)


def _utc(value: object) -> datetime:
    if isinstance(value, pd.Timestamp):
        result = value.to_pydatetime()
    elif isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value)
    else:
        raise RuntimeError("timestamp must be datetime-like")
    if result.tzinfo is None or result.utcoffset() is None:
        raise RuntimeError("timestamp must be timezone-aware")
    return result.astimezone(UTC)


def _base_candles(frame: pd.DataFrame) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            timestamp=pd.Timestamp(row.open_time).to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for row in frame.itertuples(index=False)
    )


def _core_timeframes(base_bars: Sequence[Candle]) -> dict[str, tuple[Candle, ...]]:
    store = TimeframeStore(
        (
            TimeframeSpec(ratio=3, output_timeframe="15m", align_to_clock=True),
            TimeframeSpec(ratio=12, output_timeframe="1h", align_to_clock=True),
            TimeframeSpec(ratio=48, output_timeframe="4h", align_to_clock=True),
            TimeframeSpec(ratio=288, output_timeframe="1d", align_to_clock=True),
        ),
        expected_base_interval=_FIVE_MINUTES,
    )
    for candle in base_bars:
        if store.push(candle):
            raise RuntimeError("Core timeframe replay observed an unexpected canonical data gap")
    return {name: tuple(series) for name, series in store.snapshot().items()}


def _producer_signal(feature: Feature) -> Signal:
    value = _mapping(feature.value, "runtime producer snapshot")
    module_id = str(value.get("module_id"))
    try:
        priority = _COMPONENT_PRIORITY[module_id]
    except KeyError as exc:
        raise RuntimeError(f"unexpected FVG-RT producer module: {module_id}") from exc
    side = str(value.get("side"))
    if side == "buy":
        historical_side = "long"
    elif side == "sell":
        historical_side = "short"
    else:
        raise RuntimeError("runtime producer emitted an unsupported side")
    return Signal(
        available_at=pd.Timestamp(_utc(value.get("signal_available_at"))),
        module_id=module_id,
        module_version=str(value.get("module_version")),
        setup_id=str(value.get("setup_id")),
        side=historical_side,
        priority=priority,
        entry=float(str(value.get("entry_price"))),
        stop=float(str(value.get("stop_price"))),
        target_r=float(str(value.get("target_r"))),
        ttl_bars=int(value.get("ttl_bars_5m")),
        max_hold_bars=int(value.get("max_hold_bars_5m")),
        no_progress_bars=int(value.get("no_progress_bars_5m")),
        no_progress_min_r=float(str(value.get("no_progress_min_mfe_r"))),
        signal_key=str(value.get("semantic_input_digest")),
    )


def _producer_view(
    *,
    base_bars: Sequence[Candle],
    base_index: int,
    timeframe_bars: Mapping[str, tuple[Candle, ...]],
    close_times: Mapping[str, tuple[datetime, ...]],
    required_timeframes: Sequence[str],
) -> MarketView:
    current = base_bars[base_index]
    visible = {
        timeframe: ReadOnlySeries(
            timeframe_bars[timeframe],
            bisect_right(close_times[timeframe], current.close_time),
        )
        for timeframe in required_timeframes
    }
    return MarketView._from_engine_snapshots(
        base_bars=(current,),
        timeframe_bars=MappingProxyType(visible),
        index=base_index,
        cash=INITIAL_EQUITY,
        equity=INITIAL_EQUITY,
        position=None,
        pending_orders=(),
        exit_pending=False,
        data_gap=False,
        features=None,
        readiness=None,
        sessions=None,
    )


def replay_runtime_producers(frame: pd.DataFrame) -> list[Signal]:
    base_bars = _base_candles(frame)
    if not base_bars:
        raise RuntimeError("canonical producer replay requires base candles")
    timeframes = _core_timeframes(base_bars)
    close_times = {
        name: tuple(candle.close_time for candle in bars) for name, bars in timeframes.items()
    }
    first_open = base_bars[0].timestamp
    modules = (
        FvgRtFinal15mModule(),
        Frozen1hShortModule(),
        Frozen4hShortModule(),
    )
    emitted: list[Signal] = []

    for module in modules:
        source_timeframe = _SOURCE_TIMEFRAME[module.name]
        source_bars = timeframes[source_timeframe]
        manifest = module.manifest
        if manifest is None:
            raise RuntimeError(f"canonical producer {module.name} has no manifest")
        for source in source_bars:
            delta = source.close_time - first_open
            base_index, remainder = divmod(
                int(delta.total_seconds()),
                int(_FIVE_MINUTES.total_seconds()),
            )
            base_index -= 1
            if remainder or base_index < 0 or base_index >= len(base_bars):
                raise RuntimeError("Core aggregated close does not map to a canonical 5m bar")
            if base_bars[base_index].close_time != source.close_time:
                raise RuntimeError("Core aggregated close-time causality drift")
            view = _producer_view(
                base_bars=base_bars,
                base_index=base_index,
                timeframe_bars=timeframes,
                close_times=close_times,
                required_timeframes=manifest.required_timeframes,
            )
            decision = module.on_bar(view)
            for feature in decision.features:
                emitted.append(_producer_signal(feature))

    emitted.sort(
        key=lambda signal: (
            signal.available_at,
            signal.priority,
            _SETUP_ORDER[signal.setup_id],
            signal.module_id,
            signal.signal_key,
        )
    )
    return emitted


def _assert_full_producer_parity(
    historical_signals: Sequence[Signal],
    producer_signals: Sequence[Signal],
) -> dict[str, int]:
    expected = list(historical_signals)
    observed = list(producer_signals)
    if observed != expected:
        for index, (left, right) in enumerate(zip(expected, observed, strict=False)):
            if left != right:
                raise RuntimeError(
                    "runtime producer replay differs from immutable historical signal corpus at "
                    f"index {index}: expected={left.signal_key}, observed={right.signal_key}"
                )
        raise RuntimeError(
            "runtime producer replay signal-count drift: "
            f"expected={len(expected)}, observed={len(observed)}"
        )
    counts = {
        module_id: sum(signal.module_id == module_id for signal in observed)
        for module_id in _COMPONENT_PRIORITY
    }
    return {
        "producer_signal_count": len(observed),
        "producer_final15_signal_count": counts["fvg_rt_final_15m"],
        "producer_frozen1h_signal_count": counts["frozen_1h_short"],
        "producer_frozen4h_signal_count": counts["frozen_4h_short"],
    }


def _bar_for_signal(frame: pd.DataFrame, available_at: object) -> tuple[int, Candle]:
    close_time = pd.Timestamp(_utc(available_at))
    open_time = close_time - pd.Timedelta(minutes=5)
    first_open = pd.Timestamp(frame["open_time"].iloc[0])
    delta_ns = (open_time - first_open).value
    interval_ns = pd.Timedelta(minutes=5).value
    index, remainder = divmod(delta_ns, interval_ns)
    if remainder or index < 0 or index >= len(frame):
        raise RuntimeError("historical signal is outside the canonical 5m frame")
    row = frame.iloc[int(index)]
    if pd.Timestamp(row["open_time"]) != open_time:
        raise RuntimeError("historical signal does not align to the canonical 5m frame")
    candle = Candle(
        timestamp=open_time.to_pydatetime(),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        symbol="BTCUSDT",
        timeframe="5m",
    )
    if candle.close_time != close_time.to_pydatetime():
        raise RuntimeError("canonical 5m close-time alignment drift")
    return int(index), candle


def _view(current: Candle, index: int, features: Sequence[Feature]) -> MarketView:
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=index,
        cash=INITIAL_EQUITY,
        equity=INITIAL_EQUITY,
        position=None,
        pending_orders=(),
        features=FeatureView(features, current.close_time),
    )


def _source_feature(signal: Signal) -> Feature:
    available_at = _utc(signal.available_at)
    if signal.module_id == "fvg_rt_final_15m":
        module = FvgRtFinal15mModule()
        snapshot = module._snapshot(
            signal.setup_id,
            "BTCUSDT",
            available_at,
            signal.entry,
            signal.stop,
        )
        if snapshot is None:
            raise RuntimeError("Final15 runtime snapshot rejected an accepted historical signal")
        feature = module._feature(snapshot, available_at, "BTCUSDT")
    elif signal.module_id == "frozen_1h_short":
        module = Frozen1hShortModule()
        snapshot = module._snapshot(
            "BTCUSDT",
            available_at,
            entry=signal.entry,
            stop=signal.stop,
        )
        if snapshot is None:
            raise RuntimeError("Frozen1H runtime snapshot rejected an accepted historical signal")
        feature = module._feature(snapshot, available_at, "BTCUSDT")
    elif signal.module_id == "frozen_4h_short":
        module = Frozen4hShortModule()
        snapshot = module._snapshot(
            "BTCUSDT",
            available_at,
            entry=signal.entry,
            stop=signal.stop,
        )
        if snapshot is None:
            raise RuntimeError("Frozen4H runtime snapshot rejected an accepted historical signal")
        feature = module._feature(snapshot, available_at, "BTCUSDT")
    else:
        raise RuntimeError(f"unsupported canonical FVG-RT module: {signal.module_id}")

    value = _mapping(feature.value, "runtime producer snapshot")
    if value.get("semantic_input_digest") != signal.signal_key:
        raise RuntimeError("runtime producer semantic input digest drift")
    if value.get("module_id") != signal.module_id:
        raise RuntimeError("runtime producer module identity drift")
    if value.get("module_version") != signal.module_version:
        raise RuntimeError("runtime producer module version drift")
    if value.get("setup_id") != signal.setup_id:
        raise RuntimeError("runtime producer setup identity drift")
    return feature


def _runtime_signal(setup: Feature) -> Signal:
    payload = _mapping(setup.value, "canonical setup")
    metadata = _mapping(payload.get("metadata"), "canonical setup metadata")
    source = _mapping(metadata.get("source"), "canonical FVG-RT source snapshot")
    side = str(source.get("side"))
    if side == "buy":
        historical_side = "long"
    elif side == "sell":
        historical_side = "short"
    else:
        raise RuntimeError("canonical FVG-RT source side drift")
    priority = metadata.get("historical_priority")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise RuntimeError("canonical FVG-RT historical priority is invalid")
    return Signal(
        available_at=pd.Timestamp(_utc(source.get("signal_available_at"))),
        module_id=str(source.get("module_id")),
        module_version=str(source.get("module_version")),
        setup_id=str(source.get("setup_id")),
        side=historical_side,
        priority=priority,
        entry=float(str(source.get("entry_price"))),
        stop=float(str(source.get("stop_price"))),
        target_r=float(str(source.get("target_r"))),
        ttl_bars=int(source.get("ttl_bars_5m")),
        max_hold_bars=int(source.get("max_hold_bars_5m")),
        no_progress_bars=int(source.get("no_progress_bars_5m")),
        no_progress_min_r=float(str(source.get("no_progress_min_mfe_r"))),
        signal_key=str(source.get("semantic_input_digest")),
    )


def _setup_key(setup: Feature) -> str:
    payload = _mapping(setup.value, "canonical setup")
    metadata = _mapping(payload.get("metadata"), "canonical setup metadata")
    source = _mapping(metadata.get("source"), "canonical FVG-RT source snapshot")
    value = source.get("semantic_input_digest")
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError("canonical setup lost source signal identity")
    return value


def _features(decision_features: Sequence[Feature], key: str) -> tuple[Feature, ...]:
    return tuple(feature for feature in decision_features if feature.key == key)


def _assert_downstream_contract(
    signal: Signal,
    setup: Feature,
    entry: Feature,
    protection: Feature,
) -> None:
    setup_payload = _mapping(setup.value, "canonical setup")
    metadata = _mapping(setup_payload.get("metadata"), "canonical setup metadata")
    setup_order = _SETUP_ORDER[signal.setup_id]
    expected_router_priority = -(signal.priority * 10 + setup_order)
    if metadata.get("priority") != expected_router_priority:
        raise RuntimeError("canonical FVG-RT Router priority drift")
    if metadata.get("historical_priority") != signal.priority:
        raise RuntimeError("canonical FVG-RT historical component priority drift")
    if metadata.get("historical_setup_priority") != setup_order:
        raise RuntimeError("canonical FVG-RT historical setup priority drift")

    entry_payload = _mapping(entry.value, "entry plan")
    if int(entry_payload.get("ttl_bars")) != signal.ttl_bars:
        raise RuntimeError("canonical FVG-RT entry TTL drift")
    if float(str(entry_payload.get("price"))) != signal.entry:
        raise RuntimeError("canonical FVG-RT entry price drift")
    expected_expiry = _utc(signal.available_at) + _FIVE_MINUTES * signal.ttl_bars
    if _utc(entry_payload.get("expires_at")) != expected_expiry:
        raise RuntimeError("canonical FVG-RT entry expiry drift")
    entry_metadata = _mapping(entry_payload.get("metadata"), "entry metadata")
    if entry_metadata.get("priority") != expected_router_priority:
        raise RuntimeError("Entry Model lost canonical FVG-RT Router priority")
    if entry_metadata.get("module_id") != signal.module_id:
        raise RuntimeError("Entry Model lost canonical FVG-RT module identity")
    source_setup_metadata = _mapping(
        entry_metadata.get("source_setup_metadata"),
        "entry source setup metadata",
    )
    if float(str(source_setup_metadata.get("target_r"))) != signal.target_r:
        raise RuntimeError("Entry Model lost canonical FVG-RT target-R contract")

    protection_payload = _mapping(protection.value, "protection plan")
    canonical_plan = _plan_from_signal(signal)
    if float(str(protection_payload.get("stop_loss"))) != signal.stop:
        raise RuntimeError("canonical FVG-RT stop-loss drift")
    if float(str(protection_payload.get("take_profit"))) != canonical_plan.target:
        raise RuntimeError("canonical FVG-RT target drift")
    if protection_payload.get("max_holding_seconds") != signal.max_hold_bars * 300:
        raise RuntimeError("canonical FVG-RT max-hold drift")
    no_progress = _mapping(protection_payload.get("no_progress_rule"), "no-progress rule")
    if no_progress.get("after_seconds") != signal.no_progress_bars * 300:
        raise RuntimeError("canonical FVG-RT no-progress boundary drift")
    if float(str(no_progress.get("minimum_mfe_r"))) != signal.no_progress_min_r:
        raise RuntimeError("canonical FVG-RT no-progress MFE threshold drift")


def _risk_decision(current: Candle, index: int, entry: Feature, protection: Feature) -> Feature:
    composer = RiskRequestComposerModule(
        requested_risk_fraction="0.005",
        require_instrument_feature=False,
        require_portfolio_feature=False,
    )
    request_decision = composer.on_bar(_view(current, index, (entry, protection)))
    requests = _features(request_decision.features, _RISK_REQUEST_KEY)
    if len(requests) != 1:
        raise RuntimeError("canonical FVG-RT risk request was not produced exactly once")
    sizing = RiskSizingModule(
        default_risk_fraction="0.005",
        max_trade_notional_multiple="3",
        max_portfolio_notional_multiple="3",
        max_symbol_notional_multiple="3",
        max_leverage="3",
    )
    sizing_decision = sizing.on_bar(_view(current, index, requests))
    decisions = _features(sizing_decision.features, _RISK_DECISION_KEY)
    if len(decisions) != 1:
        raise RuntimeError("canonical FVG-RT risk decision was not produced exactly once")
    payload = _mapping(decisions[0].value, "risk decision")
    if payload.get("approved") is not True:
        raise RuntimeError(
            "canonical FVG-RT historical signal was rejected by canonical Risk Sizing"
        )
    return decisions[0]


def _assert_single_active_exposure(journal: Mapping[str, Any]) -> None:
    orders = journal.get("orders")
    fills = journal.get("fills")
    if not isinstance(orders, list) or not isinstance(fills, list):
        raise RuntimeError("authoritative journal exposure surface is missing")

    active_orders: set[str] = set()
    terminal_events = {"filled", "cancelled", "rejected", "expired"}
    for row in orders:
        if not isinstance(row, Mapping):
            raise RuntimeError("authoritative order event is malformed")
        event = row.get("event")
        order = row.get("order")
        if not isinstance(event, str) or not isinstance(order, Mapping):
            raise RuntimeError("authoritative order event payload is malformed")
        order_id = order.get("id")
        if not isinstance(order_id, str):
            raise RuntimeError("authoritative order event is missing order id")
        if event == "submitted":
            if active_orders:
                raise RuntimeError("canonical composition created overlapping pending orders")
            active_orders.add(order_id)
        elif event in terminal_events:
            active_orders.discard(order_id)
        if len(active_orders) > 1:
            raise RuntimeError("canonical composition exceeded one active pending order")
    if active_orders:
        raise RuntimeError("canonical composition left an active order after end-of-data cleanup")

    position_open = False
    for fill in fills:
        if not isinstance(fill, Mapping):
            raise RuntimeError("authoritative fill is malformed")
        if fill.get("reason") == "entry":
            if position_open:
                raise RuntimeError("canonical composition opened overlapping positions")
            position_open = True
        else:
            if not position_open:
                raise RuntimeError("canonical composition emitted an exit without an open position")
            position_open = False
    if position_open:
        raise RuntimeError("canonical composition left a position open after end-of-data cleanup")


def _canonical_modules() -> tuple[
    CanonicalSetupAdapterModule,
    EntryModelModule,
    ProtectionTargetModule,
    TradePlanRouterModule,
]:
    adapter = CanonicalSetupAdapterModule(
        sources=(
            {
                "feature_key": FEATURE_KEY,
                "profile": "fvg_rt_final_15m_v1",
                "source_module": "fvg_rt_final_15m",
            },
            {
                "feature_key": FROZEN_1H_FEATURE_KEY,
                "profile": "fvg_rt_frozen_1h_v1",
                "source_module": "frozen_1h_short",
            },
            {
                "feature_key": FROZEN_4H_FEATURE_KEY,
                "profile": "fvg_rt_frozen_4h_v1",
                "source_module": "frozen_4h_short",
            },
        ),
        max_processed_sources=100_000,
    )
    entry = EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_EXPLICIT,
        ttl_bars=576,
        allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
        max_processed_setups=100_000,
    )
    protection = ProtectionTargetModule(
        entry_feature_key=_ENTRY_KEY,
        invalidation_mode="entry_plan",
        invalidation_field="metadata.source_setup_metadata.invalidation_price",
        stop_buffer_bps="0",
        target_mode="structural",
        structural_target_source="entry_plan",
        target_field="metadata.source_setup_metadata.target_price",
        max_holding_seconds_field="metadata.source_setup_metadata.max_holding_seconds",
        no_progress_rule_field="metadata.source_setup_metadata.no_progress_rule",
        max_processed_plans=100_000,
    )
    router = TradePlanRouterModule(
        long_short_policy="higher_priority",
        max_portfolio_risk_fraction="0.03",
        max_symbol_notional_fraction="3",
    )
    if router.config.existing_position_policy.value != "reject":
        raise RuntimeError("canonical Router must reject new exposure with an open position")
    if router.config.pending_order_policy.value != "reject":
        raise RuntimeError("canonical Router must reject new exposure with a pending order")
    if router.config.active_plan_policy.value != "reject_new_exposure":
        raise RuntimeError("canonical Router active-plan policy drift")
    return adapter, entry, protection, router


def build_runtime_signal_plan(
    frame: pd.DataFrame,
    historical_signals: Sequence[Signal],
) -> tuple[list[Signal], dict[str, int]]:
    adapter, entry_model, protection_target, router = _canonical_modules()
    by_time: dict[datetime, list[Signal]] = defaultdict(list)
    for signal in historical_signals:
        by_time[_utc(signal.available_at)].append(signal)

    runtime: list[Signal] = []
    arbitration_groups = 0
    for available_at, incoming in sorted(by_time.items()):
        index, current = _bar_for_signal(frame, available_at)
        source_features = tuple(_source_feature(signal) for signal in incoming)
        adapted = adapter.on_bar(_view(current, index, source_features))
        setups = _features(adapted.features, "setup.confirmed")
        if len(setups) != len(incoming):
            raise RuntimeError("Setup Adapter did not preserve every historical FVG-RT signal")
        setup_by_key = {_setup_key(setup): setup for setup in setups}
        if set(setup_by_key) != {signal.signal_key for signal in incoming}:
            raise RuntimeError("Setup Adapter changed historical FVG-RT signal identity")

        entry_decision = entry_model.on_bar(_view(current, index, setups))
        entries = _features(entry_decision.features, _ENTRY_KEY)
        if len(entries) != len(incoming):
            raise RuntimeError("Entry Model did not preserve every canonical FVG-RT setup")
        entry_by_setup = {
            str(_mapping(feature.value, "entry plan").get("setup_id")): feature
            for feature in entries
        }

        protection_decision = protection_target.on_bar(_view(current, index, entries))
        protections = _features(protection_decision.features, _PROTECTION_KEY)
        if len(protections) != len(incoming):
            raise RuntimeError("Protection Target did not preserve every FVG-RT entry plan")
        protection_by_setup = {
            str(_mapping(feature.value, "protection plan").get("setup_id")): feature
            for feature in protections
        }

        risk_decisions: list[Feature] = []
        for signal in incoming:
            setup = setup_by_key[signal.signal_key]
            setup_id = str(_mapping(setup.value, "canonical setup").get("setup_id"))
            try:
                entry_feature = entry_by_setup[setup_id]
                protection_feature = protection_by_setup[setup_id]
            except KeyError as exc:
                raise RuntimeError("downstream FVG-RT setup correlation drift") from exc
            _assert_downstream_contract(signal, setup, entry_feature, protection_feature)
            risk_decisions.append(_risk_decision(current, index, entry_feature, protection_feature))
            runtime.append(_runtime_signal(setup))

        routed = router.on_bar(
            _view(
                current,
                index,
                (*entries, *protections, *risk_decisions),
            )
        )
        plans = _features(routed.features, _EXECUTABLE_PLAN_KEY)
        if len(plans) != 1:
            rejections = tuple(
                feature.value
                for feature in routed.features
                if feature.key == "trade_plan_router.rejected_plan"
            )
            raise RuntimeError(
                "Router did not select exactly one canonical FVG-RT candidate: "
                f"skips={routed.skips!r}; rejections={rejections!r}"
            )
        expected = _select_signal(tuple(_plan_from_signal(signal) for signal in incoming))
        expected_setup = setup_by_key[expected.signal_key]
        expected_setup_id = str(_mapping(expected_setup.value, "canonical setup").get("setup_id"))
        plan = _mapping(plans[0].value, "Router executable plan")
        if plan.get("setup_id") != expected_setup_id:
            raise RuntimeError(
                "Router arbitration differs from immutable historical FVG-RT ordering"
            )
        if len(incoming) > 1:
            arbitration_groups += 1

    if runtime != list(historical_signals):
        for index, (expected, observed) in enumerate(
            zip(historical_signals, runtime, strict=False)
        ):
            if expected != observed:
                raise RuntimeError(
                    "runtime FVG-RT signal contract drift at "
                    f"index {index}: {expected.signal_key} != {observed.signal_key}"
                )
        raise RuntimeError("runtime FVG-RT signal count drift")

    return runtime, {
        "signal_groups": len(by_time),
        "multi_candidate_groups": arbitration_groups,
    }


def run(
    input_path: Path,
    baseline_dir: Path,
    output_dir: Path,
    *,
    generated_from_commit: str,
    provenance_path: Path,
) -> dict[str, object]:
    provenance = _load_provenance(provenance_path, input_path)
    if provenance.get("generated_from_commit") != generated_from_commit:
        raise RuntimeError("dataset provenance commit mismatch")
    frame = load_5m(input_path, expected_sha256=str(provenance["parquet_sha256"]))
    if len(frame) != 721_597:
        raise RuntimeError("canonical historical corpus row-count drift")

    historical_signals = build_signals(frame)
    producer_signals = replay_runtime_producers(frame)
    producer_evidence = _assert_full_producer_parity(historical_signals, producer_signals)
    runtime_signals, group_evidence = build_runtime_signal_plan(frame, historical_signals)

    reference_registered = register_signal_plan(historical_signals)
    reference_digest = reference_registered.digest
    unregister_signal_plan(reference_digest)

    registered = register_signal_plan(runtime_signals)
    if registered.digest != reference_digest:
        unregister_signal_plan(registered.digest)
        raise RuntimeError("runtime FVG-RT signal-plan digest differs from historical oracle")

    baseline, baseline_summary = _load_baseline(baseline_dir)
    _assert_reference_identity(baseline_summary)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="modular-fvg-composition-parity-") as temp_name:
            temp_root = Path(temp_name)
            module_config = temp_root / "modules.yml"
            _write_module_config(module_config, registered.digest)
            journal, core_run_id, restart_verified, final_equity = _run_one(
                input_path=input_path,
                module_config=module_config,
                output_root=temp_root / "authoritative",
                checkpoint=True,
            )
            if not restart_verified:
                raise RuntimeError("modular FVG composition restart verification did not pass")
            _assert_core_fill_contract(journal)
            _assert_order_contract(journal, registered)
            _assert_single_active_exposure(journal)
            authoritative = _authoritative_ledger(journal, frame, registered)
            _assert_ledger_exact(baseline, authoritative)
            metrics = _assert_metrics_exact(baseline_summary, authoritative)
            _exact_float(
                metrics["final_equity"],
                final_equity,
                "modular_fvg_composition.final_equity",
            )
            authoritative.to_csv(
                output_dir / "modular-comparison-ledger.csv",
                index=False,
                float_format="%.17g",
                lineterminator="\n",
            )
            report: dict[str, object] = {
                "schema_version": 1,
                "status": "MODULAR_FVG_COMPOSITION_AUTHORITATIVE_PARITY",
                "generated_from_commit": generated_from_commit,
                "paper_only": True,
                "live_trading": False,
                "candidate_id": CANDIDATE_ID,
                "composition_hash": COMPOSITION_HASH,
                "reference_semantic_digest": REFERENCE_SEMANTIC_DIGEST,
                "signal_input_source": "immutable_historical_signal_corpus",
                "producer_detection_full_corpus_replay": True,
                "producer_snapshot_contract_exact": True,
                **producer_evidence,
                "setup_adapter_contract_exact": True,
                "entry_protection_contract_exact": True,
                "risk_sizing_contract_accepted": True,
                "router_arbitration_exact": True,
                "single_active_order_position_policy": True,
                "signal_plan_digest": registered.digest,
                "signal_count": len(runtime_signals),
                **group_evidence,
                "trade_count": len(authoritative),
                "bars_processed": len(frame),
                "core_run_id": core_run_id,
                "restart_verified": True,
                "ledger_exact": True,
                "metrics_exact": True,
                "metrics": metrics,
                "authoritative_boundary": (
                    "immutable historical signals are validated through canonical runtime "
                    "producer snapshots and downstream adapters; fills/trades/PnL/fees/"
                    "slippage/lifecycle economics are emitted by modular_backtest/modular_core"
                ),
            }
            (output_dir / "composition-parity-report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            return report
    finally:
        unregister_signal_plan(registered.digest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    parser.add_argument("--dataset-provenance", type=Path, required=True)
    arguments = parser.parse_args()
    report = run(
        arguments.input.resolve(strict=True),
        arguments.baseline.resolve(strict=True),
        arguments.output.resolve(),
        generated_from_commit=arguments.generated_from_commit,
        provenance_path=arguments.dataset_provenance.resolve(strict=True),
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
