from __future__ import annotations

import pytest

from modular_trading_runner import ConfigurationError, ExecutionMode, TradingRunnerConfig


def valid_config() -> dict[str, object]:
    return {
        "schema_version": "1",
        "runtime_id": "runner-1",
        "portfolio_id": "portfolio-1",
        "strategy_id": "strategy-1",
        "execution_mode": "paper",
        "base_timeframe": "5m",
        "market": "usdt_perpetual",
        "instruments": ("BTCUSDT",),
        "market_data_sources": ("binance-usdtm",),
        "modules": ({"class": "example:Module"},),
    }


def test_config_is_strict_paper_only_and_hash_is_stable() -> None:
    first = TradingRunnerConfig.from_mapping(valid_config())
    second = TradingRunnerConfig.from_mapping(dict(reversed(tuple(valid_config().items()))))
    assert first.execution_mode is ExecutionMode.PAPER
    assert first.canonical_hash == second.canonical_hash
    assert len(first.canonical_hash) == 64


@pytest.mark.parametrize("mode", ["live", "real", "production_trading", True, None])
def test_live_or_invalid_execution_modes_fail_closed(mode: object) -> None:
    payload = valid_config()
    payload["execution_mode"] = mode
    with pytest.raises(ConfigurationError, match="exactly 'paper'"):
        TradingRunnerConfig.from_mapping(payload)


def test_unknown_fields_fail_closed() -> None:
    payload = valid_config()
    payload["allow_live"] = False
    with pytest.raises(ConfigurationError, match="unknown configuration keys"):
        TradingRunnerConfig.from_mapping(payload)


def test_market_identity_sections_are_required() -> None:
    payload = valid_config()
    payload.pop("market_data_sources")
    with pytest.raises(ConfigurationError, match="market_data_sources"):
        TradingRunnerConfig.from_mapping(payload)
