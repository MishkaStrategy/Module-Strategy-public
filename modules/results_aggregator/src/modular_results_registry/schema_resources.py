from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Mapping

from .canonical import canonical_sha256, deep_freeze
from .errors import ValidationError

_SCHEMA_PACKAGE = "modular_results_registry.schemas"
_CATALOG_NAME = "catalog.json"
_CATALOG_FIELDS = frozenset({"catalog_hash", "schema_version", "schemas"})
_CATALOG_ENTRY_FIELDS = frozenset({"id", "path"})


def _decode_json_object(data: bytes, name: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    try:
        value = json.loads(data, parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValidationError(f"schema resource is not valid JSON: {name}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"schema resource must contain an object: {name}")
    return value


def _resource_json_names(root: Any) -> tuple[str, ...]:
    return tuple(sorted(item.name for item in root.iterdir() if item.name.endswith(".json")))


def _validated_schema_names(root: Any) -> tuple[str, ...]:
    actual_names = _resource_json_names(root)
    if _CATALOG_NAME not in actual_names:
        raise ValidationError("schema catalog resource is missing")

    catalog_resource = root.joinpath(_CATALOG_NAME)
    if not catalog_resource.is_file():
        raise ValidationError("schema catalog resource is not a regular file")
    catalog = _decode_json_object(catalog_resource.read_bytes(), _CATALOG_NAME)
    if set(catalog) != _CATALOG_FIELDS:
        raise ValidationError("schema catalog fields are invalid")
    if catalog.get("schema_version") != "1":
        raise ValidationError("unsupported schema catalog version")

    expected_catalog_hash = catalog.get("catalog_hash")
    if not isinstance(expected_catalog_hash, str) or len(expected_catalog_hash) != 64:
        raise ValidationError("schema catalog hash is invalid")
    catalog_payload = {
        "schema_version": catalog["schema_version"],
        "schemas": catalog["schemas"],
    }
    if canonical_sha256(catalog_payload) != expected_catalog_hash:
        raise ValidationError("schema catalog hash mismatch")

    entries = catalog.get("schemas")
    if not isinstance(entries, list):
        raise ValidationError("schema catalog entries must be a list")
    paths: list[str] = []
    identifiers: list[str] = []
    for raw in entries:
        if not isinstance(raw, dict) or set(raw) != _CATALOG_ENTRY_FIELDS:
            raise ValidationError("schema catalog entry fields are invalid")
        path = raw.get("path")
        identifier = raw.get("id")
        if (
            not isinstance(path, str)
            or not path.endswith(".schema.json")
            or path != path.rsplit("/", 1)[-1]
        ):
            raise ValidationError("schema catalog path is invalid")
        if not isinstance(identifier, str) or not identifier:
            raise ValidationError("schema catalog id is invalid")
        paths.append(path)
        identifiers.append(identifier)

    if paths != sorted(paths):
        raise ValidationError("schema catalog paths are not sorted")
    if len(paths) != len(set(paths)) or len(identifiers) != len(set(identifiers)):
        raise ValidationError("schema catalog paths and ids must be unique")
    actual_schema_names = tuple(name for name in actual_names if name != _CATALOG_NAME)
    if tuple(paths) != actual_schema_names:
        raise ValidationError("schema catalog inventory does not match packaged resources")

    for path, identifier in zip(paths, identifiers, strict=True):
        resource = root.joinpath(path)
        if not resource.is_file():
            raise ValidationError(f"schema resource is missing: {path}")
        document = _decode_json_object(resource.read_bytes(), path)
        if document.get("$id") != identifier:
            raise ValidationError(f"schema catalog id mismatch: {path}")

    return actual_names


def list_schema_names() -> tuple[str, ...]:
    return _validated_schema_names(files(_SCHEMA_PACKAGE))


def read_schema_bytes(name: str) -> bytes:
    if not name or name != name.rsplit("/", 1)[-1] or not name.endswith(".json"):
        raise ValidationError("schema name must be a JSON basename")
    root = files(_SCHEMA_PACKAGE)
    _validated_schema_names(root)
    resource = root.joinpath(name)
    if not resource.is_file():
        raise ValidationError(f"unknown schema resource: {name}")
    return resource.read_bytes()


def read_schema(name: str) -> Mapping[str, Any]:
    return deep_freeze(_decode_json_object(read_schema_bytes(name), name))


def schema_hash(name: str) -> str:
    return canonical_sha256(read_schema(name))
