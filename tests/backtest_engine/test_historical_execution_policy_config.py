from __future__ import annotations

import json
from pathlib import Path

from modular_backtest.config import load_config


def test_backtest_yaml_exposes_historical_execution_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "data": {
                    "path": "bars.csv",
                    "format": "csv",
                    "symbol": "BTCUSDT",
                },
                "modules": "modules.yaml",
                "execution": {
                    "slippage_bps": 5.0,
                    "stop_slippage_bps": 7.0,
                    "allow_limit_stop_on_entry_bar": True,
                    "close_request_timing": "current_close",
                },
                "risk": {"sizing_model": "historical_limit_cost"},
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    core = config.core_engine_config()

    assert core.execution.allow_limit_stop_on_entry_bar is True
    assert core.execution.close_request_timing == "current_close"
    assert core.execution.slippage_bps == 5.0
    assert core.execution.stop_slippage_bps == 7.0
    assert core.risk.sizing_model == "historical_limit_cost"
