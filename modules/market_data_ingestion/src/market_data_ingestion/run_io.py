from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import IngestionResumeSpec, MarketDataRequest
from .enums import DataType, MarketType
from .errors import StorageError
from .integrity import RegisteredPathError, resolve_registered_path
from .manifest_io import manifest_from_dict
from .registry import descriptor_from_dict
from .serialization import read_json


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise StorageError(
            "RESUME_SPEC_INVALID",
            f"{name} must be an object with string keys",
            False,
            "resume",
        )
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list | tuple):
        raise StorageError(
            "RESUME_SPEC_INVALID",
            f"{name} must be an array",
            False,
            "resume",
        )
    return tuple(value)


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError(
            "RESUME_SPEC_INVALID",
            f"{name} must be an ISO-8601 string",
            False,
            "resume",
        )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(
            "RESUME_SPEC_INVALID",
            f"{name} is not valid ISO-8601",
            False,
            "resume",
        ) from exc


def request_from_dict(value: object) -> MarketDataRequest:
    data = _mapping(value, "request")
    start_value = data.get("start")
    return MarketDataRequest(
        request_id=str(data["request_id"]),
        exchange_id=str(data["exchange_id"]),
        market_type=MarketType(str(data["market_type"])),
        symbol=str(data["symbol"]),
        required_data=tuple(
            DataType(str(item))
            for item in _sequence(data["required_data"], "request.required_data")
        ),
        optional_data=tuple(
            DataType(str(item))
            for item in _sequence(data["optional_data"], "request.optional_data")
        ),
        base_timeframe=str(data["base_timeframe"]),
        derived_timeframes=tuple(
            str(item)
            for item in _sequence(data["derived_timeframes"], "request.derived_timeframes")
        ),
        start=_time(start_value, "request.start") if start_value is not None else None,
        end=_time(data["end"], "request.end"),
        full_available_history=bool(data.get("full_available_history", False)),
        allow_provisional=bool(data.get("allow_provisional", False)),
        schema_version=int(data.get("schema_version", 1)),
    )


def resume_spec_from_dict(value: object) -> IngestionResumeSpec:
    data = _mapping(value, "resume spec")
    base_dataset_value = data.get("base_dataset")
    base_manifest_value = data.get("base_manifest_override")
    return IngestionResumeSpec(
        run_id=str(data["run_id"]),
        execution_hash=str(data["execution_hash"]),
        request=request_from_dict(data["request"]),
        adapter_version=str(data["adapter_version"]),
        storage_format=str(data["storage_format"]),
        compression=str(data["compression"]),
        reject_unresolved_required_gaps=bool(data["reject_unresolved_required_gaps"]),
        created_at=_time(data["created_at"], "created_at"),
        concurrency=int(data.get("concurrency", 1)),
        max_records_in_memory=int(data.get("max_records_in_memory", 5_000_000)),
        base_dataset=(
            descriptor_from_dict(dict(_mapping(base_dataset_value, "base_dataset")))
            if base_dataset_value is not None
            else None
        ),
        base_manifest_override=(
            manifest_from_dict(_mapping(base_manifest_value, "base_manifest_override"))
            if base_manifest_value is not None
            else None
        ),
        resume_spec_version=int(data.get("resume_spec_version", 1)),
    )


def load_resume_spec(storage_root: Path, run_id: str) -> IngestionResumeSpec:
    root = storage_root.expanduser().resolve()
    uri = f"runs/{run_id}.json"
    try:
        path = resolve_registered_path(root, uri, kind="resume spec")
    except RegisteredPathError as exc:
        raise StorageError(
            "RESUME_SPEC_PATH_UNSAFE",
            "resume spec path is unsafe",
            False,
            "resume",
        ) from exc
    if not path.is_file():
        raise StorageError(
            "RESUME_SPEC_MISSING",
            "run has no persisted resume specification",
            False,
            "resume",
            {"run_id": run_id},
        )
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        raise StorageError(
            "RESUME_SPEC_CORRUPT",
            "resume specification cannot be decoded",
            False,
            "resume",
            {"run_id": run_id},
        ) from exc
    spec = resume_spec_from_dict(payload)
    if spec.run_id != run_id:
        raise StorageError(
            "RESUME_SPEC_IDENTITY_MISMATCH",
            "resume specification run ID does not match requested run",
            False,
            "resume",
        )
    return spec
