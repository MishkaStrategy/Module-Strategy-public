from __future__ import annotations

from dataclasses import dataclass, field

from .validation import finite_number, nonnegative_number, positive_number

_RUN_IDENTITY_OMIT_DEFAULT = "run_identity_omit_default"


@dataclass(frozen=True, slots=True)
class FeeConfig:
    maker_rate: float = 0.0002
    taker_rate: float = 0.0005

    def __post_init__(self) -> None:
        object.__setattr__(self, "maker_rate", nonnegative_number(self.maker_rate, "maker_rate"))
        object.__setattr__(self, "taker_rate", nonnegative_number(self.taker_rate, "taker_rate"))


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    slippage_bps: float = 0.0
    stop_slippage_bps: float = 0.0
    intrabar_priority: str = "stop"  # stop | target
    limit_fill_policy: str = "at_limit"  # at_limit | price_improvement
    allow_exit_on_entry_bar: bool = False
    allow_limit_stop_on_entry_bar: bool = field(
        default=False,
        metadata={_RUN_IDENTITY_OMIT_DEFAULT: True},
    )
    allow_reentry_on_exit_bar: bool = False
    close_request_timing: str = field(
        default="next_open",
        metadata={_RUN_IDENTITY_OMIT_DEFAULT: True},
    )
    target_liquidity: str = "maker"  # maker | taker
    max_fill_fraction_per_bar: float = 1.0
    max_fill_quantity_per_bar: float | None = None
    stop_limit_same_bar_policy: str = "defer"  # defer | fill

    def __post_init__(self) -> None:
        _require_string(self.intrabar_priority, "intrabar_priority")
        _require_string(self.limit_fill_policy, "limit_fill_policy")
        _require_string(self.close_request_timing, "close_request_timing")
        _require_string(self.target_liquidity, "target_liquidity")
        _require_string(self.stop_limit_same_bar_policy, "stop_limit_same_bar_policy")
        _require_bool(self.allow_exit_on_entry_bar, "allow_exit_on_entry_bar")
        _require_bool(self.allow_limit_stop_on_entry_bar, "allow_limit_stop_on_entry_bar")
        _require_bool(self.allow_reentry_on_exit_bar, "allow_reentry_on_exit_bar")
        if self.intrabar_priority not in {"stop", "target"}:
            raise ValueError("intrabar_priority must be 'stop' or 'target'")
        if self.limit_fill_policy not in {"at_limit", "price_improvement"}:
            raise ValueError("limit_fill_policy must be 'at_limit' or 'price_improvement'")
        if self.close_request_timing not in {"next_open", "current_close"}:
            raise ValueError("close_request_timing must be 'next_open' or 'current_close'")
        if self.target_liquidity not in {"maker", "taker"}:
            raise ValueError("target_liquidity must be 'maker' or 'taker'")
        if self.stop_limit_same_bar_policy not in {"defer", "fill"}:
            raise ValueError("stop_limit_same_bar_policy must be 'defer' or 'fill'")
        object.__setattr__(
            self,
            "slippage_bps",
            nonnegative_number(self.slippage_bps, "slippage_bps"),
        )
        object.__setattr__(
            self,
            "stop_slippage_bps",
            nonnegative_number(self.stop_slippage_bps, "stop_slippage_bps"),
        )
        fill_fraction = finite_number(self.max_fill_fraction_per_bar, "max_fill_fraction_per_bar")
        if not 0 < fill_fraction <= 1:
            raise ValueError("max_fill_fraction_per_bar must be in (0, 1]")
        object.__setattr__(self, "max_fill_fraction_per_bar", fill_fraction)
        if self.max_fill_quantity_per_bar is not None:
            object.__setattr__(
                self,
                "max_fill_quantity_per_bar",
                positive_number(self.max_fill_quantity_per_bar, "max_fill_quantity_per_bar"),
            )


@dataclass(frozen=True, slots=True)
class RiskConfig:
    default_risk_fraction: float = 0.005
    max_risk_fraction: float = 0.05
    max_notional_multiple: float = 3.0
    min_notional: float = 0.0
    min_quantity: float = 0.0
    quantity_step: float = 0.0
    min_stop_distance_pct: float = 0.0
    max_stop_distance_pct: float | None = None
    sizing_model: str = field(
        default="stop_distance",
        metadata={_RUN_IDENTITY_OMIT_DEFAULT: True},
    )

    def __post_init__(self) -> None:
        _require_string(self.sizing_model, "sizing_model")
        if self.sizing_model not in {"stop_distance", "historical_limit_cost"}:
            raise ValueError("sizing_model must be 'stop_distance' or 'historical_limit_cost'")
        default_risk_fraction = finite_number(self.default_risk_fraction, "default_risk_fraction")
        max_risk_fraction = finite_number(self.max_risk_fraction, "max_risk_fraction")
        max_notional_multiple = positive_number(self.max_notional_multiple, "max_notional_multiple")
        normalized_nonnegative = {
            name: nonnegative_number(getattr(self, name), name)
            for name in (
                "min_notional",
                "min_quantity",
                "quantity_step",
                "min_stop_distance_pct",
            )
        }
        if not 0 < default_risk_fraction <= max_risk_fraction <= 1:
            raise ValueError("risk fractions must satisfy 0 < default <= max <= 1")
        max_stop_distance_pct = None
        if self.max_stop_distance_pct is not None:
            max_stop_distance_pct = positive_number(
                self.max_stop_distance_pct, "max_stop_distance_pct"
            )
            if max_stop_distance_pct < normalized_nonnegative["min_stop_distance_pct"]:
                raise ValueError("max_stop_distance_pct cannot be below minimum")
        object.__setattr__(self, "default_risk_fraction", default_risk_fraction)
        object.__setattr__(self, "max_risk_fraction", max_risk_fraction)
        object.__setattr__(self, "max_notional_multiple", max_notional_multiple)
        for name, value in normalized_nonnegative.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "max_stop_distance_pct", max_stop_distance_pct)


@dataclass(frozen=True, slots=True)
class DataConfig:
    expected_interval_seconds: int | None = None
    gap_policy: str = "allow"  # allow | reject
    timestamp_semantics: str = "open"  # open only in v0.3
    max_base_history: int | None = None

    def __post_init__(self) -> None:
        _optional_positive_int(self.expected_interval_seconds, "expected_interval_seconds")
        _require_string(self.gap_policy, "gap_policy")
        _require_string(self.timestamp_semantics, "timestamp_semantics")
        if self.gap_policy not in {"allow", "reject"}:
            raise ValueError("gap_policy must be 'allow' or 'reject'")
        if self.timestamp_semantics != "open":
            raise ValueError("core defines Candle.timestamp as bar open time")
        _optional_positive_int(self.max_base_history, "max_base_history")


@dataclass(frozen=True, slots=True)
class EngineConfig:
    initial_cash: float = 10_000.0
    one_active_order: bool = True
    one_position: bool = True
    allow_scale_in: bool = False
    close_open_position_at_end: bool = True
    strict_data_validation: bool = True
    strict_module_errors: bool = True
    deterministic_order_ids: bool = True
    strict_feature_contracts: bool = True
    strict_setup_transitions: bool = True
    fees: FeeConfig = field(default_factory=FeeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data: DataConfig = field(default_factory=DataConfig)

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_cash", positive_number(self.initial_cash, "initial_cash"))
        for name in (
            "one_active_order",
            "one_position",
            "allow_scale_in",
            "close_open_position_at_end",
            "strict_data_validation",
            "strict_module_errors",
            "deterministic_order_ids",
            "strict_feature_contracts",
            "strict_setup_transitions",
        ):
            _require_bool(getattr(self, name), name)
        _require_instance(self.fees, FeeConfig, "fees")
        _require_instance(self.execution, ExecutionConfig, "execution")
        _require_instance(self.risk, RiskConfig, "risk")
        _require_instance(self.data, DataConfig, "data")
        if not self.one_position:
            raise ValueError("core v0.3 supports one net position; one_position must remain true")
        if (
            self.risk.sizing_model == "historical_limit_cost"
            and self.execution.limit_fill_policy != "at_limit"
        ):
            raise ValueError(
                "historical_limit_cost sizing requires execution.limit_fill_policy = at_limit"
            )


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")


def _require_string(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")


def _require_instance(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be {expected.__name__}")


def _optional_positive_int(value: int | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    if value < 1:
        raise ValueError(f"{name} must be positive")
