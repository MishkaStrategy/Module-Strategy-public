from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from .domain import InstrumentRules


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    strategy_id: str
    version: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    enabled: bool
    rules: InstrumentRules
    strategy: StrategyConfig
    chart_enabled: bool = True


@dataclass(frozen=True, slots=True)
class BotConfig:
    database: Path
    chart_theme: str
    instruments: tuple[InstrumentConfig, ...]
    enabled: bool = True


def load_config(path: str | Path) -> BotConfig:
    config_path = Path(path).expanduser().resolve()
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ConfigurationError("config root must be a mapping")
    unknown = set(payload) - {"enabled", "state", "charts", "instruments"}
    if unknown:
        raise ConfigurationError(f"unknown config keys: {', '.join(sorted(unknown))}")
    enabled = _boolean(payload.get("enabled", True), "enabled")
    state = _mapping(payload.get("state", {}), "state")
    charts = _mapping(payload.get("charts", {}), "charts")
    rows = _mapping(payload.get("instruments", {}), "instruments")
    result = []
    for symbol, raw in rows.items():
        row = _mapping(raw, f"instruments.{symbol}")
        allowed = {
            "enabled",
            "market",
            "tick_size",
            "step_size",
            "min_price",
            "min_notional",
            "strategy",
            "notifications",
        }
        extra = set(row) - allowed
        if extra:
            raise ConfigurationError(
                f"unknown instrument keys for {symbol}: {', '.join(sorted(extra))}"
            )
        strategy = _mapping(row.get("strategy", {}), f"instruments.{symbol}.strategy")
        if "class" in strategy:
            raise ConfigurationError("arbitrary strategy import paths are forbidden")
        if set(strategy) - {"id", "version", "parameters"}:
            raise ConfigurationError(f"unknown strategy keys for {symbol}")
        notifications = _mapping(
            row.get("notifications", {}), f"instruments.{symbol}.notifications"
        )
        rules = InstrumentRules(
            str(symbol),
            str(row.get("market", "")),
            Decimal(str(row["tick_size"])),
            Decimal(str(row.get("step_size", "0"))),
            _decimal(row.get("min_price")),
            _decimal(row.get("min_notional")),
        )
        result.append(
            InstrumentConfig(
                _boolean(row.get("enabled", True), f"instruments.{symbol}.enabled"),
                rules,
                StrategyConfig(
                    str(strategy.get("id", "")),
                    str(strategy.get("version", "")),
                    _mapping(strategy.get("parameters", {}), "parameters"),
                ),
                _boolean(
                    notifications.get("chart_enabled", True),
                    f"instruments.{symbol}.notifications.chart_enabled",
                ),
            )
        )
    theme = str(charts.get("theme", "dark"))
    if theme not in {"dark", "light"}:
        raise ConfigurationError("charts.theme must be dark or light")
    database_value = state.get("database", "data/signal_bot.sqlite3")
    if not isinstance(database_value, str) or not database_value.strip():
        raise ConfigurationError("state.database must be a non-empty path string")
    database = Path(database_value).expanduser()
    if not database.is_absolute():
        database = config_path.parent / database
    return BotConfig(
        database.resolve(),
        theme,
        tuple(result),
        enabled,
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a boolean")
    return value


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))
