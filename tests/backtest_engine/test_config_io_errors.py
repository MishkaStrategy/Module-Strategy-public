from pathlib import Path

import pytest

from modular_backtest.config import ConfigurationError, load_config


def test_missing_configuration_file_is_wrapped(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read valid YAML"):
        load_config(tmp_path / "missing.yaml")


def test_malformed_yaml_is_wrapped(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("data: [", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="cannot read valid YAML"):
        load_config(path)
