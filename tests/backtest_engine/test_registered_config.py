from pathlib import Path

import pytest

from modular_backtest.config import ConfigurationError, DataSourceConfig, load_config


def test_registry_config_resolves_storage_and_exact_dataset_version(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
data:
  storage_root: registry
  dataset_id: binance_usdt-m-perpetual_btcusdt_5m
  dataset_version: 1
  symbol: BTCUSDT
modules: modules.yaml
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.data.is_registered is True
    assert config.data.path is None
    assert config.data.format is None
    assert config.data.storage_root == (tmp_path / "registry").resolve()
    assert config.data.dataset_id == "binance_usdt-m-perpetual_btcusdt_5m"
    assert config.data.dataset_version == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": Path("bars.parquet"), "format": "parquet", "storage_root": Path("data")},
        {"storage_root": Path("data"), "dataset_id": "dataset"},
        {"storage_root": Path("data"), "dataset_id": "dataset", "dataset_version": 0},
        {"storage_root": Path("data"), "dataset_id": "Unsafe Dataset", "dataset_version": 1},
    ],
)
def test_registry_config_rejects_ambiguous_or_incomplete_sources(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {"symbol": "BTCUSDT"}
    values.update(kwargs)
    with pytest.raises(ConfigurationError):
        DataSourceConfig(**values)  # type: ignore[arg-type]
