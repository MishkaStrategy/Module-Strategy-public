import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    ConfigurationError,
    DataSourceConfig,
    OutputConfig,
    load_config,
)


def _write_config(path: Path, extra: str = "") -> None:
    path.write_text(
        f"""
data:
  path: bars.csv
  format: csv
  symbol: BTCUSDT
  start: 2024-01-01T00:00:00Z
  end: 2024-02-01T00:00:00Z
modules: modules.yaml
initial_capital: 12500
requested_timeframes: [15m, 1h]
execution:
  intrabar_priority: stop
fees:
  maker_rate: 0.0002
risk:
  default_risk_fraction: 0.005
instrument:
  tick_size: 0.1
output:
  directory: artifacts
seed: 17
checkpoint:
  mode: replay
  restart_at_bar: 3
end_of_test_policy: leave_open
{extra}
""",
        encoding="utf-8",
    )


def test_valid_config_resolves_paths_and_core_contracts(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path)
    config = load_config(path)
    assert config.data.path == (tmp_path / "bars.csv").resolve()
    assert (
        config.data.start is not None
        and config.data.start.isoformat() == "2024-01-01T00:00:00+00:00"
    )
    assert (
        config.data.end is not None and config.data.end.isoformat() == "2024-02-01T00:00:00+00:00"
    )
    assert config.modules == (tmp_path / "modules.yaml").resolve()
    assert config.output.directory == (tmp_path / "artifacts").resolve()
    assert config.seed == 17
    assert config.requested_timeframes == ("15m", "1h")
    assert config.checkpoint == CheckpointConfig(mode="replay", restart_at_bar=3)
    assert config.core_engine_config().close_open_position_at_end is False
    assert config.core_engine_config().data.expected_interval_seconds == 300
    assert config.instrument_spec().symbol == "BTCUSDT"
    assert config.instrument_spec().market_type == "linear_futures"


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, "unknown: true")
    with pytest.raises(ConfigurationError, match="unknown keys"):
        load_config(path)


def test_spot_market_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="USDT-M perpetual"):
        DataSourceConfig(
            path=tmp_path / "bars.csv",
            format="csv",
            symbol="BTCUSDT",
            market_type="spot",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"format": "json"}, "format"),
        ({"base_timeframe": "1m"}, "5m"),
        ({"gap_policy": "skip"}, "gap_policy"),
        ({"symbol": "BTCUSD"}, "uppercase alphanumeric"),
        ({"symbol": "USDT"}, "uppercase alphanumeric"),
        ({"symbol": "btcusdt"}, "uppercase alphanumeric"),
        ({"symbol": "BTC-USDT"}, "uppercase alphanumeric"),
        ({"columns": {"open": "open"}}, "exactly"),
        (
            {
                "columns": {
                    "open": "price",
                    "high": "price",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                }
            },
            "distinct physical",
        ),
        (
            {
                "timestamp_column": "open",
                "columns": {
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                },
            },
            "distinct physical",
        ),
        ({"timestamp_column": ""}, "non-empty strings"),
    ],
)
def test_invalid_data_source_contracts(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "path": tmp_path / "bars.csv",
        "format": "csv",
        "symbol": "BTCUSDT",
    }
    values.update(kwargs)
    with pytest.raises(ConfigurationError, match=message):
        DataSourceConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"initial_capital": 0}, "finite positive"),
        ({"initial_capital": True}, "finite positive"),
        ({"initial_capital": float("nan")}, "finite positive"),
        ({"requested_timeframes": ("15m", "15m")}, "unique"),
        ({"requested_timeframes": ("30m",)}, "unsupported"),
        ({"seed": True}, "seed"),
        ({"end_of_test_policy": "drop"}, "end_of_test_policy"),
    ],
)
def test_invalid_application_contracts(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "data": DataSourceConfig(path=tmp_path / "bars.csv", format="csv", symbol="BTCUSDT"),
        "modules": tmp_path / "modules.yaml",
    }
    values.update(kwargs)
    with pytest.raises(ConfigurationError, match=message):
        BacktestAppConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("missing", ["path", "format", "symbol"])
def test_missing_required_data_keys_are_rejected(tmp_path: Path, missing: str) -> None:
    data = {"path": "bars.csv", "format": "csv", "symbol": "BTCUSDT"}
    del data[missing]
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps({"data": data, "modules": "modules.yaml"}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="missing required keys"):
        load_config(path)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"data": {"path": "", "format": "csv", "symbol": "BTCUSDT"}, "modules": "m"},
            "data.path",
        ),
        (
            {
                "data": {"path": "bars.csv", "format": "csv", "symbol": "BTCUSDT"},
                "modules": "",
            },
            "modules",
        ),
        (
            {
                "data": {"path": "bars.csv", "format": "csv", "symbol": "BTCUSDT"},
                "modules": "m",
                "output": {"directory": ""},
            },
            "output.directory",
        ),
        (
            {
                "data": {"path": "bars.csv", "format": "csv", "symbol": "BTCUSDT"},
                "modules": "m",
                "requested_timeframes": "15m",
            },
            "requested_timeframes",
        ),
        (
            {
                "data": {"path": "bars.csv", "format": "csv", "symbol": "BTCUSDT"},
                "modules": "m",
                "initial_capital": True,
            },
            "initial_capital",
        ),
    ],
)
def test_malformed_yaml_values_are_rejected(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "snapshot"}, "mode"),
        ({"mode": "replay", "restart_at_bar": -1}, "non-negative"),
        ({"mode": "replay", "restart_at_bar": True}, "integer"),
        ({"mode": "disabled", "restart_at_bar": 2}, "requires"),
    ],
)
def test_invalid_checkpoint_contracts(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        CheckpointConfig(**kwargs)  # type: ignore[arg-type]


def test_unknown_checkpoint_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    _write_config(path, "checkpoint:\n  mode: replay\n  restart_at_bar: 3\n  unsafe: true")
    with pytest.raises(ConfigurationError, match="checkpoint"):
        load_config(path)


def test_output_path_expands_and_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert OutputConfig(Path("~/out")).directory == tmp_path / "out"


def test_invalid_date_range_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="later"):
        DataSourceConfig(
            path=tmp_path / "bars.csv",
            format="csv",
            symbol="BTCUSDT",
            start=datetime(2024, 2, 1, tzinfo=UTC),
            end=datetime(2024, 1, 1, tzinfo=UTC),
        )
