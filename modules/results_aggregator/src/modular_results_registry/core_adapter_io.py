from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifact_store import FileLimits, read_verified_bytes
from .errors import IntegrityError, ResourceLimitError


def _load_json_object(path: Path, *, limits: FileLimits) -> dict[str, Any]:
    from . import core_adapter as core_adapter_facade

    max_json_bytes = core_adapter_facade._MAX_JSON_BYTES
    json_limits = FileLimits(
        max_file_bytes=min(limits.max_file_bytes, max_json_bytes),
        chunk_bytes=limits.chunk_bytes,
    )
    try:
        _, data = read_verified_bytes(path, limits=json_limits)
    except ResourceLimitError as exc:
        raise ResourceLimitError(f"JSON artifact exceeds {max_json_bytes} bytes") from exc

    def reject_constant(value: str) -> None:
        raise IntegrityError(f"non-finite JSON constant is forbidden: {value}")

    try:
        value = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"invalid JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"{path.name} must contain a JSON object")
    return value


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrityError(f"{name} must be a JSON object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IntegrityError(f"{name} must be a non-empty string")
    return value


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntegrityError(f"{name} must be a non-negative integer")
    return value
