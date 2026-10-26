from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise
from typing import Any

from .canonical import deep_freeze, fingerprint
from .decimal_utils import ZERO, nonnegative_decimal, positive_decimal
from .enums import AdjustmentAction, QuantityKind, ScalingMode, TriggerKind
from .models import ScalingRule


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _optional_positive(value: object | None, name: str) -> Decimal | None:
    return None if value is None else positive_decimal(value, name)


def _optional_nonnegative(value: object | None, name: str) -> Decimal | None:
    return None if value is None else nonnegative_decimal(value, name)


@dataclass(frozen=True, slots=True)
class PositionScalingConfig:
    enabled: bool = False
    rules: tuple[ScalingRule, ...] = ()
    allow_averaging_down: bool = False
    allow_pyramiding: bool = False
    allow_fixed_dca: bool = False
    allow_weighted_dca: bool = False
    allow_scale_out: bool = True
    allow_rebalance: bool = False
    allow_martingale: bool = False
    max_scale_in_count: int = 2
    max_scale_out_count: int = 4
    max_total_quantity: Decimal | None = None
    max_total_notional: Decimal | None = None
    max_total_leverage: Decimal = Decimal("3")
    max_total_position_risk_fraction: Decimal = Decimal("0.005")
    max_additional_risk_fraction: Decimal = Decimal("0.0025")
    max_position_drawdown_fraction: Decimal = Decimal("0.02")
    min_distance_between_additions_fraction: Decimal = Decimal("0.001")
    min_bars_between_additions: int = 1
    max_holding_bars_for_new_additions: int = 500
    minimum_liquidation_distance_fraction: Decimal = Decimal("0.05")
    maximum_margin_usage_fraction: Decimal = Decimal("0.5")
    maximum_average_price_deterioration_fraction: Decimal = Decimal("0.05")
    minimum_residual_quantity: Decimal = ZERO
    require_liquidation_distance: bool = True
    require_margin_usage: bool = True
    require_setup_validity: bool = True
    require_market_regime: bool = False
    allowed_market_regimes: tuple[str, ...] = ()
    setup_validity_feature: str = "setup.validity"
    market_regime_feature: str = "market_regime.state"
    atr_feature: str = "market.atr"
    instrument_rules_feature: str = "instrument.rules"
    portfolio_risk_feature: str = "portfolio.risk_snapshot"
    scaling_context_feature: str = "position_scaling.context"
    instrument_rules: Mapping[str, Any] | None = None
    max_history: int = 128
    max_processed_events: int = 1024
    emit_status_feature: bool = True
    suspend_additions_on_gap: bool = True
    block_additions_when_exit_pending: bool = True
    _fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "enabled",
            "allow_averaging_down",
            "allow_pyramiding",
            "allow_fixed_dca",
            "allow_weighted_dca",
            "allow_scale_out",
            "allow_rebalance",
            "allow_martingale",
            "require_liquidation_distance",
            "require_margin_usage",
            "require_setup_validity",
            "require_market_regime",
            "emit_status_feature",
            "suspend_additions_on_gap",
            "block_additions_when_exit_pending",
        ):
            _strict_bool(getattr(self, name), name)
        for name in ("max_scale_in_count", "max_scale_out_count", "min_bars_between_additions"):
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), name))
        for name in ("max_holding_bars_for_new_additions", "max_history", "max_processed_events"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name))
        for name in (
            "max_total_leverage",
            "max_total_position_risk_fraction",
            "max_additional_risk_fraction",
            "max_position_drawdown_fraction",
            "min_distance_between_additions_fraction",
            "minimum_liquidation_distance_fraction",
            "maximum_margin_usage_fraction",
            "maximum_average_price_deterioration_fraction",
        ):
            object.__setattr__(self, name, positive_decimal(getattr(self, name), name))
        object.__setattr__(
            self,
            "minimum_residual_quantity",
            nonnegative_decimal(self.minimum_residual_quantity, "minimum_residual_quantity"),
        )
        if self.max_total_quantity is not None:
            object.__setattr__(
                self,
                "max_total_quantity",
                positive_decimal(self.max_total_quantity, "max_total_quantity"),
            )
        if self.max_total_notional is not None:
            object.__setattr__(
                self,
                "max_total_notional",
                positive_decimal(self.max_total_notional, "max_total_notional"),
            )
        for name in (
            "max_total_position_risk_fraction",
            "max_additional_risk_fraction",
            "max_position_drawdown_fraction",
            "min_distance_between_additions_fraction",
            "minimum_liquidation_distance_fraction",
            "maximum_margin_usage_fraction",
            "maximum_average_price_deterioration_fraction",
        ):
            if getattr(self, name) > Decimal("1"):
                raise ValueError(f"{name} must be <= 1")
        if self.max_additional_risk_fraction > self.max_total_position_risk_fraction:
            raise ValueError(
                "max_additional_risk_fraction cannot exceed max_total_position_risk_fraction"
            )
        if len({rule.stage for rule in self.rules}) != len(self.rules):
            raise ValueError("rule stages must be unique")
        ordered = tuple(sorted(self.rules, key=lambda item: item.stage))
        object.__setattr__(self, "rules", ordered)
        if not self.allow_martingale:
            weighted = [
                rule.quantity_value for rule in ordered if rule.mode is ScalingMode.WEIGHTED_DCA
            ]
            if any(current > previous for previous, current in pairwise(weighted)):
                raise ValueError(
                    "increasing weighted DCA is martingale-like and disabled by default"
                )
        for rule in ordered:
            enabled = {
                ScalingMode.AVERAGING_DOWN: self.allow_averaging_down,
                ScalingMode.PYRAMIDING: self.allow_pyramiding,
                ScalingMode.FIXED_DCA: self.allow_fixed_dca,
                ScalingMode.WEIGHTED_DCA: self.allow_weighted_dca,
                ScalingMode.SCALE_OUT: self.allow_scale_out,
                ScalingMode.REBALANCE: self.allow_rebalance,
            }[rule.mode]
            if not enabled:
                raise ValueError(f"rule mode {rule.mode.value} is disabled")
        for name in (
            "setup_validity_feature",
            "market_regime_feature",
            "atr_feature",
            "instrument_rules_feature",
            "portfolio_risk_feature",
            "scaling_context_feature",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or "." not in value:
                raise ValueError(f"{name} must be a namespaced feature key")
        regimes: list[str] = []
        for item in self.allowed_market_regimes:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("allowed_market_regimes must contain non-blank strings")
            if item != item.strip():
                raise ValueError("allowed_market_regimes cannot contain surrounding whitespace")
            regimes.append(item)
        if len(regimes) != len(set(regimes)):
            raise ValueError("allowed_market_regimes must be unique")
        object.__setattr__(self, "allowed_market_regimes", tuple(sorted(regimes)))
        if self.instrument_rules is not None:
            if not isinstance(self.instrument_rules, Mapping):
                raise TypeError("instrument_rules must be a mapping")
            object.__setattr__(self, "instrument_rules", deep_freeze(self.instrument_rules))
        object.__setattr__(self, "_fingerprint", fingerprint(self.canonical_mapping()))

    @classmethod
    def from_mapping(cls, parameters: Mapping[str, Any] | None = None) -> PositionScalingConfig:
        payload = dict(parameters or {})
        allowed = {
            "enabled",
            "rules",
            "allow_averaging_down",
            "allow_pyramiding",
            "allow_fixed_dca",
            "allow_weighted_dca",
            "allow_scale_out",
            "allow_rebalance",
            "allow_martingale",
            "max_scale_in_count",
            "max_scale_out_count",
            "max_total_quantity",
            "max_total_notional",
            "max_total_leverage",
            "max_total_position_risk_fraction",
            "max_additional_risk_fraction",
            "max_position_drawdown_fraction",
            "min_distance_between_additions_fraction",
            "min_bars_between_additions",
            "max_holding_bars_for_new_additions",
            "minimum_liquidation_distance_fraction",
            "maximum_margin_usage_fraction",
            "maximum_average_price_deterioration_fraction",
            "minimum_residual_quantity",
            "require_liquidation_distance",
            "require_margin_usage",
            "require_setup_validity",
            "require_market_regime",
            "allowed_market_regimes",
            "setup_validity_feature",
            "market_regime_feature",
            "atr_feature",
            "instrument_rules_feature",
            "portfolio_risk_feature",
            "scaling_context_feature",
            "instrument_rules",
            "max_history",
            "max_processed_events",
            "emit_status_feature",
            "suspend_additions_on_gap",
            "block_additions_when_exit_pending",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown position scaling parameters: {', '.join(unknown)}")
        rules_raw = payload.pop("rules", ())
        if isinstance(rules_raw, (str, bytes)) or not isinstance(rules_raw, Sequence):
            raise TypeError("rules must be a sequence")
        rules = tuple(_rule_from_mapping(item) for item in rules_raw)
        regimes_raw = payload.get("allowed_market_regimes", ())
        if isinstance(regimes_raw, (str, bytes)) or not isinstance(regimes_raw, Sequence):
            raise TypeError("allowed_market_regimes must be a sequence")
        payload["allowed_market_regimes"] = tuple(regimes_raw)
        payload["rules"] = rules
        return cls(**payload)

    def canonical_mapping(self) -> Mapping[str, Any]:
        return {
            "enabled": self.enabled,
            "rules": tuple(rule.to_mapping() for rule in self.rules),
            "allow_averaging_down": self.allow_averaging_down,
            "allow_pyramiding": self.allow_pyramiding,
            "allow_fixed_dca": self.allow_fixed_dca,
            "allow_weighted_dca": self.allow_weighted_dca,
            "allow_scale_out": self.allow_scale_out,
            "allow_rebalance": self.allow_rebalance,
            "allow_martingale": self.allow_martingale,
            "max_scale_in_count": self.max_scale_in_count,
            "max_scale_out_count": self.max_scale_out_count,
            "max_total_quantity": self.max_total_quantity,
            "max_total_notional": self.max_total_notional,
            "max_total_leverage": self.max_total_leverage,
            "max_total_position_risk_fraction": self.max_total_position_risk_fraction,
            "max_additional_risk_fraction": self.max_additional_risk_fraction,
            "max_position_drawdown_fraction": self.max_position_drawdown_fraction,
            "min_distance_between_additions_fraction": self.min_distance_between_additions_fraction,
            "min_bars_between_additions": self.min_bars_between_additions,
            "max_holding_bars_for_new_additions": self.max_holding_bars_for_new_additions,
            "minimum_liquidation_distance_fraction": self.minimum_liquidation_distance_fraction,
            "maximum_margin_usage_fraction": self.maximum_margin_usage_fraction,
            "maximum_average_price_deterioration_fraction": self.maximum_average_price_deterioration_fraction,
            "minimum_residual_quantity": self.minimum_residual_quantity,
            "require_liquidation_distance": self.require_liquidation_distance,
            "require_margin_usage": self.require_margin_usage,
            "require_setup_validity": self.require_setup_validity,
            "require_market_regime": self.require_market_regime,
            "allowed_market_regimes": self.allowed_market_regimes,
            "setup_validity_feature": self.setup_validity_feature,
            "market_regime_feature": self.market_regime_feature,
            "atr_feature": self.atr_feature,
            "instrument_rules_feature": self.instrument_rules_feature,
            "portfolio_risk_feature": self.portfolio_risk_feature,
            "scaling_context_feature": self.scaling_context_feature,
            "instrument_rules": self.instrument_rules,
            "max_history": self.max_history,
            "max_processed_events": self.max_processed_events,
            "emit_status_feature": self.emit_status_feature,
            "suspend_additions_on_gap": self.suspend_additions_on_gap,
            "block_additions_when_exit_pending": self.block_additions_when_exit_pending,
        }

    @property
    def fingerprint(self) -> str:
        return self._fingerprint


def _rule_from_mapping(value: object) -> ScalingRule:
    if isinstance(value, ScalingRule):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("each rule must be a mapping")
    allowed = {
        "stage",
        "mode",
        "action",
        "trigger_kind",
        "trigger_value",
        "quantity_kind",
        "quantity_value",
        "desired_entry_kind",
        "explicit_price",
        "cooldown_bars",
        "expires_bars",
        "feature_key",
        "metadata",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown scaling rule fields: {', '.join(unknown)}")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise TypeError("rule metadata must be a mapping")
    return ScalingRule(
        stage=_positive_int(value.get("stage"), "stage"),
        mode=ScalingMode(str(value.get("mode", ""))),
        action=AdjustmentAction(str(value.get("action", ""))),
        trigger_kind=TriggerKind(str(value.get("trigger_kind", ""))),
        trigger_value=nonnegative_decimal(value.get("trigger_value", 0), "trigger_value"),
        quantity_kind=QuantityKind(str(value.get("quantity_kind", ""))),
        quantity_value=positive_decimal(value.get("quantity_value"), "quantity_value"),
        desired_entry_kind=str(value.get("desired_entry_kind", "limit")),
        explicit_price=_optional_positive(value.get("explicit_price"), "explicit_price"),
        cooldown_bars=_nonnegative_int(value.get("cooldown_bars", 1), "cooldown_bars"),
        expires_bars=_nonnegative_int(value.get("expires_bars", 1), "expires_bars"),
        feature_key=None if value.get("feature_key") is None else str(value["feature_key"]),
        metadata=metadata,
    )
