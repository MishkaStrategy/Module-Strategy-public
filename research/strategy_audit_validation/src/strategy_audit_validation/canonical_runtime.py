"""Deterministic research-only adapters for the canonical Variant 2 strategy."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal

from .canonical import canonical_sha256
from .errors import EvidenceMalformedError, EvidenceTamperedError

Side = Literal["long", "short"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATE_SCHEMA: Final = "canonical-strategy-state-v1"
_OUTPUT_CONTRACT: Final = "canonical-strategy-order-plan-v1"


@dataclass(frozen=True, slots=True)
class SetupRules:
    side: Side
    required_conditions: frozenset[str]
    required_completed_timeframes: frozenset[str]
    target_r: float
    ttl_bars_5m: int
    max_hold_bars_5m: int
    no_progress_bars_5m: int
    no_progress_min_mfe_r: float

    def __post_init__(self) -> None:
        if not self.required_conditions:
            raise EvidenceMalformedError("setup requires at least one condition")
        if not self.required_completed_timeframes:
            raise EvidenceMalformedError("setup requires completed timeframes")
        _positive_finite(self.target_r, "target_r")
        _positive_int(self.ttl_bars_5m, "ttl_bars_5m")
        _positive_int(self.max_hold_bars_5m, "max_hold_bars_5m")
        _positive_int(self.no_progress_bars_5m, "no_progress_bars_5m")
        _positive_finite(self.no_progress_min_mfe_r, "no_progress_min_mfe_r")
        if self.no_progress_bars_5m >= self.max_hold_bars_5m:
            raise EvidenceMalformedError("no-progress check must precede maximum hold")


@dataclass(frozen=True, slots=True)
class ModuleRules:
    module_id: str
    module_version: str
    priority: int
    setups: Mapping[str, SetupRules]

    def __post_init__(self) -> None:
        if not self.module_id or self.module_id != self.module_id.strip():
            raise EvidenceMalformedError("module_id must be non-blank and trimmed")
        if not self.module_version or self.module_version != self.module_version.strip():
            raise EvidenceMalformedError("module_version must be non-blank and trimmed")
        _positive_int(self.priority, "priority")
        if not self.setups:
            raise EvidenceMalformedError("module requires at least one setup")
        object.__setattr__(self, "setups", MappingProxyType(dict(self.setups)))


@dataclass(frozen=True, slots=True)
class SignalSnapshot:
    module_id: str
    module_version: str
    setup_id: str
    signal_available_at: datetime
    first_eligible_5m_open: datetime
    conditions: Mapping[str, bool]
    completed_timeframes: frozenset[str]
    entry_price: float
    stop_price: float
    semantic_input_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))
        _sha256(self.semantic_input_digest, "semantic_input_digest")


@dataclass(frozen=True, slots=True)
class OrderPlan:
    output_contract: str
    module_id: str
    module_version: str
    setup_id: str
    side: Side
    signal_available_at: datetime
    first_eligible_5m_open: datetime
    entry_price: float
    stop_price: float
    target_price: float
    target_r: float
    ttl_bars_5m: int
    max_hold_bars_5m: int
    no_progress_bars_5m: int
    no_progress_min_mfe_r: float
    priority: int
    fill_rule: str
    same_bar_priority: tuple[str, ...]
    semantic_input_digest: str
    plan_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    schema_version: str
    composition_hash: str
    candidate_id: str
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    symbol: str
    last_processed_5m_open: datetime
    state_sequence: int
    pending_order_digest: str | None
    open_position_digest: str | None
    checkpoint_digest: str


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise EvidenceMalformedError(f"{name} must be a positive integer")
    return value


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise EvidenceMalformedError(f"{name} must be positive and finite")
    return value


def _sha256(value: str, name: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
    return value


def _optional_sha(value: str | None, name: str) -> str | None:
    return None if value is None else _sha256(value, name)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceMalformedError(f"{name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise EvidenceMalformedError(f"{name} must use UTC")
    return value.astimezone(UTC)


_COMMON_COMPLETED: Final = frozenset({"5m"})
MODULE_RULES: Final[Mapping[str, ModuleRules]] = MappingProxyType(
    {
        "fvg_rt_final_15m": ModuleRules(
            module_id="fvg_rt_final_15m",
            module_version="2.0.0",
            priority=1,
            setups={
                "long": SetupRules(
                    side="long",
                    required_conditions=frozenset(
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
                    required_completed_timeframes=_COMMON_COMPLETED | {"15m", "4h"},
                    target_r=4.5,
                    ttl_bars_5m=36,
                    max_hold_bars_5m=288,
                    no_progress_bars_5m=48,
                    no_progress_min_mfe_r=0.15,
                ),
                "short_a": SetupRules(
                    side="short",
                    required_conditions=frozenset(
                        {
                            "all_inputs_completed",
                            "atr_fraction_inclusive",
                            "bearish_fvg",
                            "completed_1d_close_below_ema200",
                            "completed_4h_trend_down_3",
                            "fvg_size_atr_inclusive",
                            "impulse_efficiency_min",
                            "middle_body_atr_min",
                            "middle_bearish",
                            "short_location_4h_max",
                            "volume_ratio_min",
                        }
                    ),
                    required_completed_timeframes=_COMMON_COMPLETED | {"15m", "4h", "1d"},
                    target_r=5.0,
                    ttl_bars_5m=48,
                    max_hold_bars_5m=288,
                    no_progress_bars_5m=24,
                    no_progress_min_mfe_r=0.35,
                ),
                "short_b": SetupRules(
                    side="short",
                    required_conditions=frozenset(
                        {
                            "all_inputs_completed",
                            "atr_fraction_inclusive",
                            "bearish_bos_48",
                            "bearish_fvg",
                            "close_strength_min",
                            "completed_1d_close_below_ema200",
                            "fvg_size_atr_inclusive",
                            "impulse_efficiency_min",
                            "middle_body_atr_min",
                            "middle_bearish",
                            "no_recent_bullish_fvg_12",
                            "rsi14_max",
                            "short_location_48h_min",
                            "short_location_8h_min",
                            "volume_ratio_min",
                        }
                    ),
                    required_completed_timeframes=_COMMON_COMPLETED | {"15m", "1d"},
                    target_r=6.0,
                    ttl_bars_5m=48,
                    max_hold_bars_5m=576,
                    no_progress_bars_5m=60,
                    no_progress_min_mfe_r=0.35,
                ),
            },
        ),
        "frozen_1h_short": ModuleRules(
            module_id="frozen_1h_short",
            module_version="1.0.0",
            priority=2,
            setups={
                "short": SetupRules(
                    side="short",
                    required_conditions=frozenset(
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
                    ),
                    required_completed_timeframes=_COMMON_COMPLETED | {"1h", "4h"},
                    target_r=6.0,
                    ttl_bars_5m=48,
                    max_hold_bars_5m=144,
                    no_progress_bars_5m=72,
                    no_progress_min_mfe_r=0.5,
                )
            },
        ),
        "frozen_4h_short": ModuleRules(
            module_id="frozen_4h_short",
            module_version="1.0.0",
            priority=3,
            setups={
                "short": SetupRules(
                    side="short",
                    required_conditions=frozenset(
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
                    ),
                    required_completed_timeframes=_COMMON_COMPLETED | {"4h"},
                    target_r=6.0,
                    ttl_bars_5m=576,
                    max_hold_bars_5m=576,
                    no_progress_bars_5m=432,
                    no_progress_min_mfe_r=0.35,
                )
            },
        ),
    }
)


class ContractBoundAdapter:
    """Convert an exact feature/signal snapshot into a deterministic order plan."""

    def __init__(self, module_id: str) -> None:
        try:
            self._rules = MODULE_RULES[module_id]
        except KeyError as exc:
            raise EvidenceMalformedError(f"unknown canonical module: {module_id}") from exc

    def build_plan(self, snapshot: SignalSnapshot) -> OrderPlan:
        rules = self._rules
        if snapshot.module_id != rules.module_id:
            raise EvidenceMalformedError("snapshot module_id mismatch")
        if snapshot.module_version != rules.module_version:
            raise EvidenceMalformedError("snapshot module_version mismatch")
        try:
            setup = rules.setups[snapshot.setup_id]
        except KeyError as exc:
            raise EvidenceMalformedError("unknown setup_id for module") from exc
        available_at = _utc(snapshot.signal_available_at, "signal_available_at")
        first_open = _utc(snapshot.first_eligible_5m_open, "first_eligible_5m_open")
        if first_open != available_at:
            raise EvidenceMalformedError("first eligible 5m open must equal signal availability")
        if snapshot.completed_timeframes != setup.required_completed_timeframes:
            raise EvidenceMalformedError("completed timeframe identity mismatch")
        if set(snapshot.conditions) != set(setup.required_conditions):
            raise EvidenceMalformedError("condition identity mismatch")
        if not all(snapshot.conditions.values()):
            raise EvidenceMalformedError("failed condition cannot produce an order plan")
        entry = _positive_finite(snapshot.entry_price, "entry_price")
        stop = _positive_finite(snapshot.stop_price, "stop_price")
        if setup.side == "long":
            initial_r = entry - stop
            if initial_r <= 0:
                raise EvidenceMalformedError("long stop must be below entry")
            target = entry + setup.target_r * initial_r
        else:
            initial_r = stop - entry
            if initial_r <= 0:
                raise EvidenceMalformedError("short stop must be above entry")
            target = entry - setup.target_r * initial_r
        if target <= 0 or not math.isfinite(target):
            raise EvidenceMalformedError("target price must be positive and finite")
        payload = {
            "entry_price": entry,
            "fill_rule": "touch_inclusive_at_configured_limit",
            "first_eligible_5m_open": first_open,
            "max_hold_bars_5m": setup.max_hold_bars_5m,
            "module_id": rules.module_id,
            "module_version": rules.module_version,
            "no_progress_bars_5m": setup.no_progress_bars_5m,
            "no_progress_min_mfe_r": setup.no_progress_min_mfe_r,
            "output_contract": _OUTPUT_CONTRACT,
            "priority": rules.priority,
            "same_bar_priority": ("stop", "full_target", "close_based_no_progress_exit"),
            "semantic_input_digest": snapshot.semantic_input_digest,
            "setup_id": snapshot.setup_id,
            "side": setup.side,
            "signal_available_at": available_at,
            "stop_price": stop,
            "target_price": target,
            "target_r": setup.target_r,
            "ttl_bars_5m": setup.ttl_bars_5m,
        }
        return OrderPlan(
            output_contract=_OUTPUT_CONTRACT,
            module_id=rules.module_id,
            module_version=rules.module_version,
            setup_id=snapshot.setup_id,
            side=setup.side,
            signal_available_at=available_at,
            first_eligible_5m_open=first_open,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            target_r=setup.target_r,
            ttl_bars_5m=setup.ttl_bars_5m,
            max_hold_bars_5m=setup.max_hold_bars_5m,
            no_progress_bars_5m=setup.no_progress_bars_5m,
            no_progress_min_mfe_r=setup.no_progress_min_mfe_r,
            priority=rules.priority,
            fill_rule="touch_inclusive_at_configured_limit",
            same_bar_priority=("stop", "full_target", "close_based_no_progress_exit"),
            semantic_input_digest=snapshot.semantic_input_digest,
            plan_digest=canonical_sha256(payload),
        )


def build_checkpoint(
    *,
    composition_hash: str,
    dataset_id: str,
    dataset_version: int,
    dataset_hash: str,
    last_processed_5m_open: datetime,
    state_sequence: int,
    pending_order_digest: str | None = None,
    open_position_digest: str | None = None,
) -> RuntimeCheckpoint:
    composition = _sha256(composition_hash, "composition_hash")
    dataset = _sha256(dataset_hash, "dataset_hash")
    _positive_int(dataset_version, "dataset_version")
    if not dataset_id or dataset_id != dataset_id.strip():
        raise EvidenceMalformedError("dataset_id must be non-blank and trimmed")
    if state_sequence < 0:
        raise EvidenceMalformedError("state_sequence must be non-negative")
    pending = _optional_sha(pending_order_digest, "pending_order_digest")
    position = _optional_sha(open_position_digest, "open_position_digest")
    last_open = _utc(last_processed_5m_open, "last_processed_5m_open")
    candidate_id = f"strategy-candidate-{composition[:24]}"
    payload = {
        "candidate_id": candidate_id,
        "composition_hash": composition,
        "dataset_hash": dataset,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "last_processed_5m_open": last_open,
        "open_position_digest": position,
        "pending_order_digest": pending,
        "schema_version": _STATE_SCHEMA,
        "state_sequence": state_sequence,
        "symbol": "BTCUSDT",
    }
    return RuntimeCheckpoint(
        schema_version=_STATE_SCHEMA,
        composition_hash=composition,
        candidate_id=candidate_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_hash=dataset,
        symbol="BTCUSDT",
        last_processed_5m_open=last_open,
        state_sequence=state_sequence,
        pending_order_digest=pending,
        open_position_digest=position,
        checkpoint_digest=canonical_sha256(payload),
    )


def validate_checkpoint(
    checkpoint: RuntimeCheckpoint,
    *,
    expected_composition_hash: str,
    expected_dataset_id: str,
    expected_dataset_version: int,
    expected_dataset_hash: str,
    minimum_state_sequence: int = 0,
) -> None:
    expected = build_checkpoint(
        composition_hash=expected_composition_hash,
        dataset_id=expected_dataset_id,
        dataset_version=expected_dataset_version,
        dataset_hash=expected_dataset_hash,
        last_processed_5m_open=checkpoint.last_processed_5m_open,
        state_sequence=checkpoint.state_sequence,
        pending_order_digest=checkpoint.pending_order_digest,
        open_position_digest=checkpoint.open_position_digest,
    )
    if checkpoint.schema_version != _STATE_SCHEMA or checkpoint.symbol != "BTCUSDT":
        raise EvidenceTamperedError("checkpoint schema or symbol mismatch")
    if checkpoint.state_sequence < minimum_state_sequence:
        raise EvidenceTamperedError("checkpoint state sequence regressed")
    if checkpoint != expected:
        raise EvidenceTamperedError("checkpoint identity or digest mismatch")


__all__ = [
    "MODULE_RULES",
    "ContractBoundAdapter",
    "ModuleRules",
    "OrderPlan",
    "RuntimeCheckpoint",
    "SetupRules",
    "SignalSnapshot",
    "build_checkpoint",
    "validate_checkpoint",
]
