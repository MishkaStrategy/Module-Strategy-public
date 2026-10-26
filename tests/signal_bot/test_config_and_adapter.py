from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modular_core import Candle, ModuleDecision, Signal, Side, StrategyModule
from signal_bot.config import ConfigurationError, load_config
from signal_bot.domain import CandleBatch, InstrumentRules, StrategyContext
from signal_bot.strategy_adapter import ModuleSignalAdapter

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def write_config(path: Path, extra: str = "") -> Path:
    path.write_text(
        """
state:
  database: data/test.sqlite3
charts:
  theme: dark
instruments:
  BTCUSDT:
    enabled: true
    market: binance_usdt_m_perpetual
    tick_size: "0.1"
    step_size: "0.001"
    strategy:
      id: btc_strategy
      version: "1.0.0"
      parameters:
        length: 20
    notifications:
      chart_enabled: true
"""
        + extra,
        encoding="utf-8",
    )
    return path


def test_config_loads_strict_instrument(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "bot.yaml")
    config = load_config(config_path)
    assert config.enabled is True
    assert config.chart_theme == "dark"
    assert config.database == (tmp_path / "data/test.sqlite3").resolve()
    assert len(config.instruments) == 1
    item = config.instruments[0]
    assert item.rules.symbol == "BTCUSDT" and item.strategy.version == "1.0.0"
    assert item.strategy.strategy_id == "btc_strategy"
    assert item.strategy.parameters["length"] == 20


def test_config_accepts_disabled_signal_bot(tmp_path: Path) -> None:
    config_path = write_config(tmp_path / "disabled.yaml", "enabled: false\n")
    assert load_config(config_path).enabled is False


def test_config_rejects_unknown_and_invalid_shapes(tmp_path: Path) -> None:
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("unknown: true\ninstruments: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown config keys"):
        load_config(unknown)
    bad_enabled = tmp_path / "bad-enabled.yaml"
    bad_enabled.write_text("enabled: 'false'\ninstruments: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="enabled must be a boolean"):
        load_config(bad_enabled)
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("instruments: []\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be a mapping"):
        load_config(malformed)
    bad_theme = tmp_path / "theme.yaml"
    bad_theme.write_text("charts: {theme: neon}\ninstruments: {}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="dark or light"):
        load_config(bad_theme)


class DeliverableModule(StrategyModule):
    name = "deliverable"
    signal_delivery_ready = True
    portfolio_independent = True

    from modular_core import ModuleManifest

    manifest = ModuleManifest(name=name, version="1.0.0", required_timeframes=("5m",))

    def on_bar(self, view):
        return ModuleDecision()

    def evaluate_delivery(self, context):
        view = type("View", (), {"current": context.bars["5m"].candles[-1]})()
        return ModuleDecision(
            signals=(
                Signal(
                    module=self.name,
                    side=Side.BUY,
                    reason="confirmed",
                    metadata={
                        "entry_type": "limit",
                        "entry_price": "100.0",
                        "stop_loss": "99.0",
                        "take_profit_levels": ("102.0",),
                        "signal_timestamp": view.current.timestamp.isoformat(),
                        "expires_at": (view.current.close_time + timedelta(hours=1)).isoformat(),
                        "setup_id": "module-setup",
                    },
                ),
            )
        )


class AnalyticalModule(StrategyModule):
    name = "analytical"

    def on_bar(self, view):
        return ModuleDecision(signals=(Signal(module=self.name, side=Side.BUY, reason="fact"),))


class PortfolioModule(DeliverableModule):
    portfolio_independent = False


def context() -> StrategyContext:
    candle = Candle(
        NOW - timedelta(minutes=5),
        100,
        101,
        99,
        100,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    batch = CandleBatch("BTCUSDT", "5m", (candle,), NOW)
    return StrategyContext(
        InstrumentRules(
            "BTCUSDT", "binance_usdt_m_perpetual", __import__("decimal").Decimal("0.1")
        ),
        {"5m": batch},
        NOW,
    )


def test_module_adapter_accepts_levels_and_rejects_analytical_signal() -> None:
    decision = ModuleSignalAdapter(DeliverableModule()).evaluate(context())
    assert decision.has_signal and str(decision.entry_price) == "100.0"
    with pytest.raises(ValueError, match="signal_delivery_ready"):
        ModuleSignalAdapter(AnalyticalModule()).evaluate(context())
    with pytest.raises(ValueError, match="portfolio-sensitive"):
        ModuleSignalAdapter(PortfolioModule())
