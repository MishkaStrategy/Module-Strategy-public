from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .contracts import MarketDataRequest
from .enums import DataType, MarketType
from .timeframes import floor_timeframe


class ConfigurationError(ValueError):
    pass


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{name} keys must be strings")
    return value


def _only(data: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigurationError(f"unknown {name} fields: {unknown}")


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a YAML boolean")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationError(f"{name} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigurationError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class StorageConfiguration:
    root: Path
    format: str
    compression: str
    partition: str


@dataclass(frozen=True, slots=True)
class ValidationConfiguration:
    deep: bool
    repair_gaps: bool
    reject_unresolved_required_gaps: bool
    allow_partial_optional_data: bool


@dataclass(frozen=True, slots=True)
class DownloadConfiguration:
    concurrency: int
    retries: int
    timeout_seconds: int
    resume: bool
    max_records_in_memory: int


@dataclass(frozen=True, slots=True)
class IngestionConfiguration:
    request: MarketDataRequest
    storage: StorageConfiguration
    validation: ValidationConfiguration
    download: DownloadConfiguration


def load_config(path: Path, *, now: datetime | None = None) -> IngestionConfiguration:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load YAML configuration: {path}") from exc
    root = _mapping(document, "configuration")
    _only(
        root,
        {
            "schema_version",
            "exchange",
            "instrument",
            "range",
            "data",
            "download",
            "validation",
            "storage",
            "request_id",
        },
        "top-level",
    )
    if root.get("schema_version") != 1:
        raise ConfigurationError("schema_version must be 1")

    exchange = _mapping(root.get("exchange"), "exchange")
    _only(exchange, {"id", "market_type"}, "exchange")
    instrument = _mapping(root.get("instrument"), "instrument")
    _only(instrument, {"symbol"}, "instrument")
    range_config = _mapping(root.get("range"), "range")
    _only(range_config, {"mode", "start", "end"}, "range")
    data = _mapping(root.get("data"), "data")
    _only(
        data,
        {
            "base_timeframe",
            "derived_timeframes",
            "required_types",
            "optional_types",
        },
        "data",
    )
    download = _mapping(root.get("download", {}), "download")
    _only(
        download,
        {
            "concurrency",
            "retries",
            "timeout_seconds",
            "resume",
            "max_records_in_memory",
        },
        "download",
    )
    validation = _mapping(root.get("validation", {}), "validation")
    _only(
        validation,
        {
            "deep",
            "repair_gaps",
            "reject_unresolved_required_gaps",
            "allow_partial_optional_data",
        },
        "validation",
    )
    storage = _mapping(root.get("storage"), "storage")
    _only(storage, {"root", "format", "compression", "partition"}, "storage")

    current = (now or datetime.now(UTC)).astimezone(UTC)
    base_timeframe = _string(data.get("base_timeframe"), "data.base_timeframe").lower()
    end_raw = range_config.get("end", "latest_closed")
    if end_raw in {"latest_closed", "latest-closed", "latest_closed_period"}:
        end = floor_timeframe(current, base_timeframe)
    else:
        end = _time(end_raw, "range.end")
    mode = _string(range_config.get("mode", "explicit"), "range.mode")
    full = mode == "full_available_history"
    start = None if full else _time(range_config.get("start"), "range.start")

    def data_types(key: str) -> tuple[DataType, ...]:
        value = data.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ConfigurationError(f"data.{key} must be a string list")
        try:
            return tuple(DataType(item) for item in value)
        except ValueError as exc:
            raise ConfigurationError(f"data.{key} contains unsupported data type") from exc

    derived_raw = data.get("derived_timeframes", [])
    if not isinstance(derived_raw, list) or any(not isinstance(item, str) for item in derived_raw):
        raise ConfigurationError("data.derived_timeframes must be a string list")
    request = MarketDataRequest(
        request_id=_string(
            root.get(
                "request_id",
                f"mdi-{current.strftime('%Y%m%dT%H%M%S')}",
            ),
            "request_id",
        ),
        exchange_id=_string(exchange.get("id"), "exchange.id"),
        market_type=MarketType(_string(exchange.get("market_type"), "exchange.market_type")),
        symbol=_string(instrument.get("symbol"), "instrument.symbol"),
        required_data=data_types("required_types"),
        optional_data=data_types("optional_types"),
        base_timeframe=base_timeframe,
        derived_timeframes=tuple(item.lower() for item in derived_raw),
        start=start,
        end=end,
        full_available_history=full,
    )
    storage_format = _string(storage.get("format", "parquet"), "storage.format").lower()
    if storage_format not in {"parquet", "csv"}:
        raise ConfigurationError("storage.format must be parquet or csv")
    partition = _string(storage.get("partition", "monthly"), "storage.partition").lower()
    if partition != "monthly":
        raise ConfigurationError("only monthly partitioning is currently supported")
    return IngestionConfiguration(
        request=request,
        storage=StorageConfiguration(
            root=(path.parent / _string(storage.get("root", "./data"), "storage.root")).resolve(),
            format=storage_format,
            compression=_string(storage.get("compression", "zstd"), "storage.compression"),
            partition=partition,
        ),
        validation=ValidationConfiguration(
            deep=_bool(validation.get("deep", True), "validation.deep"),
            repair_gaps=_bool(validation.get("repair_gaps", True), "validation.repair_gaps"),
            reject_unresolved_required_gaps=_bool(
                validation.get("reject_unresolved_required_gaps", True),
                "validation.reject_unresolved_required_gaps",
            ),
            allow_partial_optional_data=_bool(
                validation.get("allow_partial_optional_data", True),
                "validation.allow_partial_optional_data",
            ),
        ),
        download=DownloadConfiguration(
            concurrency=_positive_int(download.get("concurrency", 4), "download.concurrency"),
            retries=_positive_int(download.get("retries", 8), "download.retries"),
            timeout_seconds=_positive_int(
                download.get("timeout_seconds", 30),
                "download.timeout_seconds",
            ),
            resume=_bool(download.get("resume", True), "download.resume"),
            max_records_in_memory=_positive_int(
                download.get("max_records_in_memory", 5_000_000),
                "download.max_records_in_memory",
            ),
        ),
    )
