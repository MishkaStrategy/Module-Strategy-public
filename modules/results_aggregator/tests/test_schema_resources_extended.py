from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

import modular_results_registry.schema_resources as resources
from modular_results_registry.canonical import canonical_sha256
from modular_results_registry.errors import ValidationError


_MODULE_ROOT = Path(__file__).resolve().parents[1]
_PACKAGED_SCHEMAS = _MODULE_ROOT / "src/modular_results_registry/schemas"


def _copy(destination: Path) -> Path:
    source = _PACKAGED_SCHEMAS
    destination.mkdir()
    for path in source.glob("*.json"):
        shutil.copyfile(path, destination / path.name)
    return destination


def _catalog(root: Path) -> dict[str, Any]:
    return json.loads((root / "catalog.json").read_text(encoding="utf-8"))


def _write_catalog(root: Path, catalog: dict[str, Any], *, rehash: bool = True) -> None:
    if rehash and set(catalog) >= {"schema_version", "schemas"}:
        catalog["catalog_hash"] = canonical_sha256(
            {"schema_version": catalog["schema_version"], "schemas": catalog["schemas"]}
        )
    (root / "catalog.json").write_text(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )


def _use(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(resources, "files", lambda package: root)


def test_schema_catalog_requires_catalog_regular_file_and_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy(tmp_path / "missing")
    (root / "catalog.json").unlink()
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="catalog resource is missing"):
        resources.list_schema_names()

    root = _copy(tmp_path / "directory")
    (root / "catalog.json").unlink()
    (root / "catalog.json").mkdir()
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="not a regular file"):
        resources.list_schema_names()

    for index, payload in enumerate((b"\xff", b"[]", b'{"value":NaN}')):
        root = _copy(tmp_path / f"invalid-{index}")
        (root / "catalog.json").write_bytes(payload)
        _use(monkeypatch, root)
        with pytest.raises(ValidationError, match="not valid JSON|must contain an object"):
            resources.list_schema_names()


@pytest.mark.parametrize(
    ("mutate", "message", "rehash"),
    [
        (lambda c: c.update(extra=True), "fields are invalid", False),
        (lambda c: c.update(schema_version="2"), "unsupported schema catalog version", False),
        (lambda c: c.update(catalog_hash=1), "catalog hash is invalid", False),
        (lambda c: c.update(catalog_hash="a"), "catalog hash is invalid", False),
        (lambda c: c.update(catalog_hash="0" * 64), "catalog hash mismatch", False),
        (lambda c: c.update(schemas={}), "entries must be a list", True),
        (lambda c: c["schemas"].__setitem__(0, {"id": "x"}), "entry fields are invalid", True),
        (lambda c: c["schemas"].__setitem__(0, []), "entry fields are invalid", True),
        (lambda c: c["schemas"][0].update(path="../x.schema.json"), "path is invalid", True),
        (lambda c: c["schemas"][0].update(path="x.json"), "path is invalid", True),
        (lambda c: c["schemas"][0].update(path=1), "path is invalid", True),
        (lambda c: c["schemas"][0].update(id=""), "id is invalid", True),
        (lambda c: c["schemas"][0].update(id=1), "id is invalid", True),
        (lambda c: c["schemas"].reverse(), "paths are not sorted", True),
        (
            lambda c: c["schemas"].__setitem__(1, dict(c["schemas"][0])),
            "paths and ids must be unique",
            True,
        ),
        (
            lambda c: c["schemas"][1].update(id=c["schemas"][0]["id"]),
            "paths and ids must be unique",
            True,
        ),
    ],
)
def test_schema_catalog_rejects_structural_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
    rehash: bool,
) -> None:
    root = _copy(tmp_path / "schemas")
    catalog = _catalog(root)
    mutate(catalog)
    _write_catalog(root, catalog, rehash=rehash)
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match=message):
        resources.list_schema_names()


def test_schema_catalog_rejects_inventory_file_type_and_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy(tmp_path / "extra")
    (root / "extra.schema.json").write_text('{"$id":"x"}', encoding="utf-8")
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="inventory"):
        resources.list_schema_names()

    root = _copy(tmp_path / "directory")
    catalog = _catalog(root)
    path = catalog["schemas"][0]["path"]
    (root / path).unlink()
    (root / path).mkdir()
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="resource is missing"):
        resources.list_schema_names()

    root = _copy(tmp_path / "id")
    catalog = _catalog(root)
    path = catalog["schemas"][0]["path"]
    document = json.loads((root / path).read_text(encoding="utf-8"))
    document["$id"] = "tampered-id"
    (root / path).write_text(json.dumps(document), encoding="utf-8")
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="id mismatch"):
        resources.list_schema_names()


def test_read_schema_rejects_unknown_regular_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _copy(tmp_path / "schemas")
    _use(monkeypatch, root)
    with pytest.raises(ValidationError, match="unknown schema resource"):
        resources.read_schema_bytes("unknown.json")
