from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from modular_results_registry.errors import ValidationError
from modular_results_registry.schema_resources import list_schema_names


_MODULE_ROOT = Path(__file__).resolve().parents[1]
_PACKAGED_SCHEMAS = _MODULE_ROOT / "src/modular_results_registry/schemas"


def _copy_packaged_schemas(destination: Path) -> None:
    source = _PACKAGED_SCHEMAS
    destination.mkdir()
    for path in source.glob("*.json"):
        shutil.copyfile(path, destination / path.name)


def test_packaged_catalog_is_complete_and_registration_schema_is_present() -> None:
    names = list_schema_names()
    assert "catalog.json" in names
    assert "registration-package.schema.json" in names
    assert len(names) == 17


def test_packaged_catalog_rejects_missing_declared_resource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import modular_results_registry.schema_resources as resources

    root = tmp_path / "schemas"
    _copy_packaged_schemas(root)
    (root / "registration-package.schema.json").unlink()
    monkeypatch.setattr(resources, "files", lambda package: root)
    with pytest.raises(ValidationError, match="inventory"):
        resources.list_schema_names()


def test_packaged_catalog_rejects_tampered_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import modular_results_registry.schema_resources as resources

    root = tmp_path / "schemas"
    _copy_packaged_schemas(root)
    catalog = root / "catalog.json"
    catalog.write_text(
        catalog.read_text().replace('"catalog_hash": "', '"catalog_hash": "0'), encoding="utf-8"
    )
    monkeypatch.setattr(resources, "files", lambda package: root)
    with pytest.raises(ValidationError, match="hash"):
        resources.list_schema_names()
