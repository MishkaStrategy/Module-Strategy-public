from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_backtest import cli
from modular_backtest.config import ConfigurationError


def test_validate_run_inspect_and_compare_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(data=object())
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    validation = SimpleNamespace(
        to_dict=lambda: {
            "rows": 3,
            "source_mode": "streaming",
            "stream_batch_size": 8192,
        }
    )
    monkeypatch.setattr(
        cli,
        "prepare_candle_source",
        lambda data: SimpleNamespace(validation=validation),
    )
    assert cli.main(["validate-data", "--config", str(tmp_path / "c.yaml")]) == 0
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload == {
        "rows": 3,
        "source_mode": "streaming",
        "stream_batch_size": 8192,
    }

    outcome = SimpleNamespace(
        run_id="run-1",
        bars_processed=3,
        output_directory=tmp_path / "out",
        restart_verified=True,
    )
    monkeypatch.setattr(cli, "run_backtest", lambda value: outcome)
    assert cli.main(["run", "--config", str(tmp_path / "c.yaml")]) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["run_id"] == "run-1"
    assert run_payload["restart_verified"] is True

    snapshot = SimpleNamespace(
        run_id="run-1",
        directory=tmp_path / "out",
        metrics={"trades": 1},
    )
    monkeypatch.setattr(cli, "inspect_result", lambda path: snapshot)
    assert cli.main(["inspect", "--result", str(tmp_path / "out")]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"] == {"trades": 1}

    monkeypatch.setattr(cli, "compare_results", lambda left, right: {"changes": {}})
    assert cli.main(["compare", "left", "right"]) == 0
    assert json.loads(capsys.readouterr().out) == {"changes": {}}


def test_cli_returns_error_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(path: Path) -> object:
        raise ConfigurationError("bad")

    monkeypatch.setattr(cli, "load_config", fail)
    assert cli.main(["validate-data", "--config", "bad.yaml"]) == 2
    assert "error: bad" in capsys.readouterr().err
