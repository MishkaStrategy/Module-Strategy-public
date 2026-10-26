from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

import strategy_factory.cli as cli
from strategy_factory.errors import StrategyFactoryError


@pytest.mark.parametrize("values", [["bad"], ["=1"], ["name="], [1]])
def test_cli_module_versions_reject_malformed_values(values) -> None:
    with pytest.raises(StrategyFactoryError, match="module-version"):
        cli._module_versions(values)  # type: ignore[arg-type]


def test_cli_module_versions_handle_duplicates_and_conflicts() -> None:
    assert cli._module_versions(["b=2", "a=1", "a=1"]) == {"a": "1", "b": "2"}
    with pytest.raises(StrategyFactoryError, match="conflicting"):
        cli._module_versions(["a=1", "a=2"])


@pytest.mark.parametrize(
    "status,code",
    [
        ("COMPLETED", 0),
        ("BLOCKED", 4),
        ("PAUSED", 4),
        ("BUDGET_EXHAUSTED", 4),
        ("DATASET_REJECTED", 3),
        ("CANCELED", 5),
        ("FAILED", 2),
    ],
)
def test_status_exit_codes(status: str, code: int) -> None:
    assert cli._status_exit_code(status) == code


def test_export_rejects_invalid_id_and_missing_artifact(tmp_path: Path) -> None:
    with pytest.raises(StrategyFactoryError, match="invalid artifact ID"):
        cli._export(tmp_path, "run", "invalid", str(tmp_path / "out"))
    with pytest.raises(StrategyFactoryError, match="artifact not found"):
        cli._export(tmp_path, "run", "type-" + "a" * 64, str(tmp_path / "out"))


class Value(Enum):
    ITEM = "item"


@dataclass
class Nested:
    value: int


def test_fallback_supports_dataclasses_collections_enums_and_scalars() -> None:
    assert cli._fallback(Nested(1)) == {"value": 1}
    assert cli._fallback({1: (Value.ITEM, {2, 1})}) == {"1": ["item", [1, 2]]} or cli._fallback(
        {1: (Value.ITEM, {2, 1})}
    ) in ({"1": ["item", [1, 2]]}, {"1": ["item", [2, 1]]})
    marker = object()
    assert cli._fallback(marker) is marker


def test_print_falls_back_when_canonical_encoding_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli, "canonical_json_bytes", lambda value: (_ for _ in ()).throw(TypeError("bad"))
    )
    cli._print(Nested(2))
    assert capsys.readouterr().out.strip() == '{"value": 2}'


def test_print_reraises_strategy_factory_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "canonical_json_bytes",
        lambda value: (_ for _ in ()).throw(StrategyFactoryError("boom")),
    )
    with pytest.raises(StrategyFactoryError, match="boom"):
        cli._print({"value": 1})


def test_application_result_serializes_step_evidence() -> None:
    result = SimpleNamespace(
        run_id="run-1",
        completed=False,
        checkpoint=SimpleNamespace(status=Value.ITEM, stage=Value.ITEM),
        steps=(
            SimpleNamespace(
                stage=Value.ITEM,
                action="blocked",
                artifact_id=None,
                error_type="ContractError",
            ),
        ),
    )
    assert cli._application_result(result)["steps"][0] == {
        "stage": "item",
        "action": "blocked",
        "artifact_id": None,
        "error_type": "ContractError",
    }


def test_main_reports_strategy_factory_error_as_json(monkeypatch, capsys, tmp_path: Path) -> None:
    class BrokenControl:
        def __init__(self, state_root, run_id):
            del state_root, run_id

        def status(self):
            raise StrategyFactoryError("broken state")

    monkeypatch.setattr(cli, "RunControl", BrokenControl)
    code = cli.main(["--state-root", str(tmp_path), "status", "--run-id", "run-1"])
    assert code == 2
    assert '"status": "ERROR"' in capsys.readouterr().err
