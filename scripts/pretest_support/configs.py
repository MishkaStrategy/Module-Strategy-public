from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .models import DataFacts, PretestError, Reporter, STEP, iso, timestamp


def _quote(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def config_text(
    data: DataFacts,
    start: datetime,
    end: datetime,
    output: str,
    checkpoint: str,
) -> str:
    columns = data.columns
    return f"""data:
  path: {_quote(data.path)}
  format: {data.format}
  symbol: BTCUSDT
  market_type: binance-usdt-m-perpetual
  base_timeframe: 5m
  gap_policy: reject
  start: {_quote(iso(start))}
  end: {_quote(iso(end))}
  timestamp_column: {_quote(data.timestamp_column)}
  columns:
    open: {_quote(columns["open"])}
    high: {_quote(columns["high"])}
    low: {_quote(columns["low"])}
    close: {_quote(columns["close"])}
    volume: {_quote(columns["volume"])}

modules: modules.yaml
initial_capital: 10000
requested_timeframes: [15m, 1h, 4h]
seed: 0
end_of_test_policy: force_close
checkpoint:
  mode: {checkpoint}
fees:
  maker_rate: 0.0002
  taker_rate: 0.00055
execution:
  slippage_bps: 2.5
  stop_slippage_bps: 2.5
  intrabar_priority: stop
  limit_fill_policy: at_limit
  allow_exit_on_entry_bar: false
  allow_reentry_on_exit_bar: false
  target_liquidity: maker
  stop_limit_same_bar_policy: defer
risk:
  default_risk_fraction: 0.005
  max_risk_fraction: 0.02
  max_notional_multiple: 3.0
instrument:
  base_asset: BTC
  quote_asset: USDT
  market_type: linear_futures
  tick_size: 0.1
  quantity_step: 0.001
  min_quantity: 0.001
  min_notional: 5
  contract_size: 1
  price_precision: 1
  quantity_precision: 3
output:
  directory: {_quote(output)}
"""


def write_configs(
    reporter: Reporter,
    repo_dir: Path,
    workspace: Path,
    data: DataFacts,
    symbol: str,
) -> dict[str, str]:
    if symbol != "BTCUSDT":
        raise PretestError("generated instrument defaults currently support BTCUSDT only")
    source = repo_dir / "configs" / "integration.example.yaml"
    if not source.is_file():
        raise PretestError(f"missing module config: {source}")
    config_dir = workspace / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "results").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, config_dir / "modules.yaml")
    first = timestamp(data.first)
    end = timestamp(data.last) + STEP
    specs = {
        "smoke": (max(first, end - timedelta(days=14)), "../results/smoke", "disabled"),
        "smoke_repeat": (
            max(first, end - timedelta(days=14)),
            "../results/smoke-repeat",
            "disabled",
        ),
        "restart_replay": (
            max(first, end - timedelta(days=90)),
            "../results/restart-replay",
            "replay",
        ),
        "full_btc": (first, "../results/full-btc", "disabled"),
    }
    generated: dict[str, str] = {}
    for name, (start, output, checkpoint) in specs.items():
        path = config_dir / f"{name}.yaml"
        path.write_text(config_text(data, start, end, output, checkpoint), encoding="utf-8")
        generated[name] = str(path.resolve())
    generated["modules"] = str((config_dir / "modules.yaml").resolve())
    commands = workspace / "commands.sh"
    commands.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python}"
"$PYTHON" -m modular_backtest validate-data --config "$ROOT/configs/smoke.yaml"
"$PYTHON" -m modular_backtest run --config "$ROOT/configs/smoke.yaml"
"$PYTHON" -m modular_backtest run --config "$ROOT/configs/smoke_repeat.yaml"
left="$(find "$ROOT/results/smoke" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
right="$(find "$ROOT/results/smoke-repeat" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
test -n "$left" && test -n "$right"
"$PYTHON" -m modular_backtest compare "$left" "$right"
"$PYTHON" -m modular_backtest run --config "$ROOT/configs/restart_replay.yaml"
""",
        encoding="utf-8",
    )
    commands.chmod(0o755)
    generated["commands"] = str(commands.resolve())
    reporter.passed("configs", "isolated smoke, repeat, replay, and full configs generated")
    return generated
