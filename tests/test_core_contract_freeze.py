from __future__ import annotations

import dataclasses
import enum
import inspect
import json
from pathlib import Path

import modular_core as core


SNAPSHOT_PATH = Path(__file__).parents[1] / "docs" / "contracts" / "core-v0.4.2.json"


def _dataclass_fields(name: str) -> list[str]:
    value = getattr(core, name)
    assert dataclasses.is_dataclass(value), name
    return [field.name for field in dataclasses.fields(value)]


def _enum_values(name: str) -> list[object]:
    value = getattr(core, name)
    assert issubclass(value, enum.Enum), name
    return [member.value for member in value]


def _signature(path: str) -> str:
    owner_name, attribute = path.split(".", 1)
    owner = core if owner_name == "modular_core" else getattr(core, owner_name)
    return str(inspect.signature(getattr(owner, attribute)))


def test_core_contract_snapshot_matches_public_surface() -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    assert core.__version__ == snapshot["version"]
    assert list(core.__all__) == snapshot["exports"]

    actual_fields = {name: _dataclass_fields(name) for name in snapshot["dataclass_fields"]}
    assert actual_fields == snapshot["dataclass_fields"]

    actual_enums = {name: _enum_values(name) for name in snapshot["enum_values"]}
    assert actual_enums == snapshot["enum_values"]

    actual_signatures = {path: _signature(path) for path in snapshot["method_signatures"]}
    assert actual_signatures == snapshot["method_signatures"]


def test_contract_snapshot_is_canonical_json() -> None:
    raw = SNAPSHOT_PATH.read_text(encoding="utf-8")
    decoded = json.loads(raw)
    assert raw == json.dumps(decoded, indent=2, ensure_ascii=False) + "\n"
