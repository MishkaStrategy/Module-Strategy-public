from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from modular_core import (
    Candle,
    DataConfig,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    RiskConfig,
)
from modular_core.run_manifest import RunManifest, build_run_manifest


@dataclass(frozen=True, slots=True)
class _LegacyExecutionConfig:
    slippage_bps: float = 0.0
    stop_slippage_bps: float = 0.0
    intrabar_priority: str = "stop"
    limit_fill_policy: str = "at_limit"
    allow_exit_on_entry_bar: bool = False
    allow_reentry_on_exit_bar: bool = False
    target_liquidity: str = "maker"
    max_fill_fraction_per_bar: float = 1.0
    max_fill_quantity_per_bar: float | None = None
    stop_limit_same_bar_policy: str = "defer"


@dataclass(frozen=True, slots=True)
class _LegacyRiskConfig:
    default_risk_fraction: float = 0.005
    max_risk_fraction: float = 0.05
    max_notional_multiple: float = 3.0
    min_notional: float = 0.0
    min_quantity: float = 0.0
    quantity_step: float = 0.0
    min_stop_distance_pct: float = 0.0
    max_stop_distance_pct: float | None = None


@dataclass(frozen=True, slots=True)
class _LegacyEngineConfig:
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
    execution: _LegacyExecutionConfig = field(default_factory=_LegacyExecutionConfig)
    risk: _LegacyRiskConfig = field(default_factory=_LegacyRiskConfig)
    data: DataConfig = field(default_factory=DataConfig)


def _manifest(config: object) -> RunManifest:
    bar = Candle(
        datetime(2026, 1, 1, tzinfo=UTC),
        100.0,
        101.0,
        99.0,
        100.5,
        1.0,
        "BTCUSDT",
        "5m",
    )
    return build_run_manifest(
        kernel_version="0.4.2",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        bars=(bar,),
        config=config,
        manifests=(),
    )


def test_additive_policy_defaults_preserve_legacy_config_hash_and_run_id() -> None:
    legacy = _manifest(_LegacyEngineConfig())
    current = _manifest(EngineConfig())

    assert current.config_hash == legacy.config_hash
    assert current.run_id == legacy.run_id


def test_historical_policy_opt_in_changes_config_identity() -> None:
    baseline = _manifest(EngineConfig())
    historical = _manifest(
        EngineConfig(
            execution=ExecutionConfig(
                allow_limit_stop_on_entry_bar=True,
                close_request_timing="current_close",
            ),
            risk=RiskConfig(sizing_model="historical_limit_cost"),
        )
    )

    assert historical.config_hash != baseline.config_hash
    assert historical.run_id != baseline.run_id
