from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext

from .constants import (
    DEFAULT_MANAGED_ORDER_TAG_PREFIX,
    DEFAULT_MAX_CANCEL_REQUESTS_PER_BAR,
    DEFAULT_MAX_TRACKED_CANCELS,
    MAX_COUNTER_VALUE,
)
from .decimal_utils import (
    CONFIG_VALIDATION_DECIMAL_CONTEXT,
    MAX_DECIMAL_TEXT_CHARS,
    exact_decimal_text,
    validate_decimal_bounds,
)
from .models import PartialExitRule, TrailingMode, TriggerPriceSource

_MAX_HOLDING_MINUTES = timedelta.max.days * 24 * 60 + timedelta.max.seconds // 60
_MAX_PARTIAL_EXIT_RULES = 64


@dataclass(frozen=True, slots=True)
class PositionExitConfig:
    max_holding_minutes: int | None = None
    partial_exits: tuple[PartialExitRule, ...] = ()
    breakeven_trigger_r: Decimal | None = None
    breakeven_offset_r: Decimal = Decimal("0")
    trailing_mode: TrailingMode = TrailingMode.NONE
    trailing_activation_r: Decimal = Decimal("1")
    trailing_distance_r: Decimal = Decimal("1")
    trailing_distance_pct: Decimal = Decimal("0.01")
    trigger_source: TriggerPriceSource = TriggerPriceSource.CLOSE
    minimum_stop_move_bps: Decimal = Decimal("1")
    market_buffer_bps: Decimal = Decimal("2")
    cancel_reduce_only_orders_on_flat: bool = True
    cancel_managed_tag_orders_on_flat: bool = True
    managed_order_tag_prefix: str = DEFAULT_MANAGED_ORDER_TAG_PREFIX
    cancel_retry_bars: int = 1
    max_tracked_cancels: int = DEFAULT_MAX_TRACKED_CANCELS
    max_cancel_requests_per_bar: int = DEFAULT_MAX_CANCEL_REQUESTS_PER_BAR
    reset_on_scale_in: bool = True
    honor_position_lifecycle_metadata: bool = False
    suspend_price_actions_on_gap: bool = True
    emit_idle_skips: bool = False
    emit_status_feature: bool = False

    def __post_init__(self) -> None:
        _optional_positive_int(self.max_holding_minutes, "max_holding_minutes")
        if self.max_holding_minutes is not None and self.max_holding_minutes > _MAX_HOLDING_MINUTES:
            raise ValueError(f"max_holding_minutes cannot exceed {_MAX_HOLDING_MINUTES}")
        if not isinstance(self.trailing_mode, TrailingMode):
            raise TypeError("trailing_mode must be TrailingMode")
        if not isinstance(self.trigger_source, TriggerPriceSource):
            raise TypeError("trigger_source must be TriggerPriceSource")
        if self.breakeven_trigger_r is not None:
            _nonnegative_decimal(self.breakeven_trigger_r, "breakeven_trigger_r")
        _nonnegative_decimal(self.breakeven_offset_r, "breakeven_offset_r")
        _nonnegative_decimal(self.trailing_activation_r, "trailing_activation_r")
        _positive_decimal(self.trailing_distance_r, "trailing_distance_r")
        _positive_decimal(self.trailing_distance_pct, "trailing_distance_pct")
        if self.trailing_distance_pct >= Decimal("1"):
            raise ValueError("trailing_distance_pct must be < 1")
        _nonnegative_decimal(self.minimum_stop_move_bps, "minimum_stop_move_bps")
        _nonnegative_decimal(self.market_buffer_bps, "market_buffer_bps")
        if self.market_buffer_bps >= Decimal("10000"):
            raise ValueError("market_buffer_bps must be < 10000")
        _bounded_positive_int(self.cancel_retry_bars, "cancel_retry_bars")
        _bounded_positive_int(self.max_tracked_cancels, "max_tracked_cancels")
        _bounded_positive_int(
            self.max_cancel_requests_per_bar,
            "max_cancel_requests_per_bar",
        )
        if self.max_cancel_requests_per_bar > self.max_tracked_cancels:
            raise ValueError("max_cancel_requests_per_bar cannot exceed max_tracked_cancels")
        for name in (
            "cancel_reduce_only_orders_on_flat",
            "cancel_managed_tag_orders_on_flat",
            "reset_on_scale_in",
            "honor_position_lifecycle_metadata",
            "suspend_price_actions_on_gap",
            "emit_idle_skips",
            "emit_status_feature",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        if (
            not isinstance(self.managed_order_tag_prefix, str)
            or not self.managed_order_tag_prefix.strip()
        ):
            raise ValueError("managed_order_tag_prefix cannot be blank")
        if self.managed_order_tag_prefix != self.managed_order_tag_prefix.strip():
            raise ValueError("managed_order_tag_prefix cannot contain surrounding whitespace")
        if len(self.managed_order_tag_prefix) > 256:
            raise ValueError("managed_order_tag_prefix exceeds 256 characters")
        if len(self.partial_exits) > _MAX_PARTIAL_EXIT_RULES:
            raise ValueError(
                f"partial_exits cannot contain more than {_MAX_PARTIAL_EXIT_RULES} rules"
            )

        for index, rule in enumerate(self.partial_exits):
            if not isinstance(rule, PartialExitRule):
                raise TypeError(f"partial_exits[{index}] must be PartialExitRule")
            validate_decimal_bounds(rule.trigger_r, f"partial_exits[{index}].trigger_r")
            validate_decimal_bounds(rule.fraction, f"partial_exits[{index}].fraction")
        ordered = tuple(sorted(self.partial_exits, key=lambda item: item.trigger_r))
        if len({rule.trigger_r for rule in ordered}) != len(ordered):
            raise ValueError("partial exit trigger_r values must be unique")
        with localcontext(CONFIG_VALIDATION_DECIMAL_CONTEXT):
            total_fraction = sum(
                (rule.fraction for rule in ordered),
                Decimal("0"),
            )
        if total_fraction > Decimal("1"):
            raise ValueError("partial exit fractions cannot exceed 1 in total")
        object.__setattr__(self, "partial_exits", ordered)

    @classmethod
    def from_parameters(
        cls,
        *,
        max_holding_minutes: int | None = None,
        partial_exits: Sequence[object] = (),
        breakeven_trigger_r: int | float | str | Decimal | None = None,
        breakeven_offset_r: int | float | str | Decimal = 0,
        trailing_mode: str | TrailingMode = TrailingMode.NONE,
        trailing_activation_r: int | float | str | Decimal = 1,
        trailing_distance_r: int | float | str | Decimal = 1,
        trailing_distance_pct: int | float | str | Decimal = "0.01",
        trigger_source: str | TriggerPriceSource = TriggerPriceSource.CLOSE,
        minimum_stop_move_bps: int | float | str | Decimal = 1,
        market_buffer_bps: int | float | str | Decimal = 2,
        cancel_reduce_only_orders_on_flat: bool = True,
        cancel_managed_tag_orders_on_flat: bool = True,
        managed_order_tag_prefix: str = DEFAULT_MANAGED_ORDER_TAG_PREFIX,
        cancel_retry_bars: int = 1,
        max_tracked_cancels: int = DEFAULT_MAX_TRACKED_CANCELS,
        max_cancel_requests_per_bar: int = DEFAULT_MAX_CANCEL_REQUESTS_PER_BAR,
        reset_on_scale_in: bool = True,
        honor_position_lifecycle_metadata: bool = False,
        suspend_price_actions_on_gap: bool = True,
        emit_idle_skips: bool = False,
        emit_status_feature: bool = False,
    ) -> PositionExitConfig:
        rules = _partial_rules(partial_exits)
        return cls(
            max_holding_minutes=max_holding_minutes,
            partial_exits=rules,
            breakeven_trigger_r=(
                None
                if breakeven_trigger_r is None
                else _decimal(breakeven_trigger_r, "breakeven_trigger_r")
            ),
            breakeven_offset_r=_decimal(breakeven_offset_r, "breakeven_offset_r"),
            trailing_mode=TrailingMode(trailing_mode),
            trailing_activation_r=_decimal(trailing_activation_r, "trailing_activation_r"),
            trailing_distance_r=_decimal(trailing_distance_r, "trailing_distance_r"),
            trailing_distance_pct=_decimal(trailing_distance_pct, "trailing_distance_pct"),
            trigger_source=TriggerPriceSource(trigger_source),
            minimum_stop_move_bps=_decimal(minimum_stop_move_bps, "minimum_stop_move_bps"),
            market_buffer_bps=_decimal(market_buffer_bps, "market_buffer_bps"),
            cancel_reduce_only_orders_on_flat=cancel_reduce_only_orders_on_flat,
            cancel_managed_tag_orders_on_flat=cancel_managed_tag_orders_on_flat,
            managed_order_tag_prefix=managed_order_tag_prefix,
            cancel_retry_bars=cancel_retry_bars,
            max_tracked_cancels=max_tracked_cancels,
            max_cancel_requests_per_bar=max_cancel_requests_per_bar,
            reset_on_scale_in=reset_on_scale_in,
            honor_position_lifecycle_metadata=honor_position_lifecycle_metadata,
            suspend_price_actions_on_gap=suspend_price_actions_on_gap,
            emit_idle_skips=emit_idle_skips,
            emit_status_feature=emit_status_feature,
        )

    def canonical_mapping(self) -> Mapping[str, object]:
        result: dict[str, object] = {
            "max_holding_minutes": self.max_holding_minutes,
            "partial_exits": tuple(
                {
                    "trigger_r": _decimal_text(rule.trigger_r),
                    "fraction": _decimal_text(rule.fraction),
                }
                for rule in self.partial_exits
            ),
            "breakeven_trigger_r": (
                None
                if self.breakeven_trigger_r is None
                else _decimal_text(self.breakeven_trigger_r)
            ),
            "breakeven_offset_r": _decimal_text(self.breakeven_offset_r),
            "trailing_mode": self.trailing_mode.value,
            "trailing_activation_r": _decimal_text(self.trailing_activation_r),
            "trailing_distance_r": _decimal_text(self.trailing_distance_r),
            "trailing_distance_pct": _decimal_text(self.trailing_distance_pct),
            "trigger_source": self.trigger_source.value,
            "minimum_stop_move_bps": _decimal_text(self.minimum_stop_move_bps),
            "market_buffer_bps": _decimal_text(self.market_buffer_bps),
            "cancel_reduce_only_orders_on_flat": self.cancel_reduce_only_orders_on_flat,
            "cancel_managed_tag_orders_on_flat": self.cancel_managed_tag_orders_on_flat,
            "managed_order_tag_prefix": self.managed_order_tag_prefix,
            "cancel_retry_bars": self.cancel_retry_bars,
            "max_tracked_cancels": self.max_tracked_cancels,
            "max_cancel_requests_per_bar": self.max_cancel_requests_per_bar,
            "reset_on_scale_in": self.reset_on_scale_in,
            "suspend_price_actions_on_gap": self.suspend_price_actions_on_gap,
            "emit_idle_skips": self.emit_idle_skips,
            "emit_status_feature": self.emit_status_feature,
        }
        if self.honor_position_lifecycle_metadata:
            result["honor_position_lifecycle_metadata"] = True
        return result

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.canonical_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _partial_rules(values: Sequence[object]) -> tuple[PartialExitRule, ...]:
    if isinstance(values, str | bytes):
        raise TypeError("partial_exits must be a sequence of mappings")
    rules: list[PartialExitRule] = []
    for index, value in enumerate(values):
        if index >= _MAX_PARTIAL_EXIT_RULES:
            raise ValueError(
                f"partial_exits cannot contain more than {_MAX_PARTIAL_EXIT_RULES} rules"
            )
        rules.append(_partial_rule(value))
    return tuple(rules)


def _partial_rule(value: object) -> PartialExitRule:
    if isinstance(value, PartialExitRule):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("each partial exit must be PartialExitRule or a mapping")
    unknown = sorted(set(value) - {"trigger_r", "fraction"})
    if unknown:
        raise ValueError(f"unknown partial exit keys: {', '.join(map(str, unknown))}")
    if "trigger_r" not in value or "fraction" not in value:
        raise ValueError("partial exit requires trigger_r and fraction")
    return PartialExitRule(
        trigger_r=_decimal(value["trigger_r"], "trigger_r"),
        fraction=_decimal(value["fraction"], "fraction"),
    )


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"{name} must be numeric")
    if isinstance(value, str) and len(value) > MAX_DECIMAL_TEXT_CHARS:
        raise ValueError(f"{name} exceeds supported Decimal text length")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TypeError(f"{name} must be numeric") from exc
    validate_decimal_bounds(result, name)
    return result


def _decimal_text(value: Decimal) -> str:
    return exact_decimal_text(value)


def _nonnegative_decimal(value: Decimal, name: str) -> None:
    validate_decimal_bounds(value, name)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


def _positive_decimal(value: Decimal, name: str) -> None:
    validate_decimal_bounds(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")


def _bounded_positive_int(value: int, name: str) -> None:
    _positive_int(value, name)
    if value > MAX_COUNTER_VALUE:
        raise ValueError(f"{name} cannot exceed {MAX_COUNTER_VALUE}")


def _optional_positive_int(value: object, name: str) -> None:
    if value is None:
        return
    _positive_int(value, name)
