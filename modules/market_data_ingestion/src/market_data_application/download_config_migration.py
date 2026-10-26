from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from market_data_ingestion.config import ConfigurationError, load_config

from .contracts import CreateDownloadRequest, StorageFormat
from .errors import ApplicationError

_MAX_SOURCE_BYTES = 1024 * 1024
_DYNAMIC_END_VALUES = frozenset({"latest_closed", "latest-closed", "latest_closed_period"})
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "apisecret",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "privatekey",
        "secret",
        "token",
    }
)
_UNREPRESENTABLE_DOWNLOAD_FIELDS = frozenset({"retries", "timeout_seconds"})
_UNREPRESENTABLE_VALIDATION_FIELDS = frozenset(
    {"allow_partial_optional_data", "deep", "repair_gaps"}
)


def migrate_legacy_download_config(
    source: Path,
    *,
    as_of: datetime | None = None,
) -> CreateDownloadRequest:
    """Convert one validated legacy YAML document to the strict v1 download request."""

    path, source_bytes = _read_safe_source(source)
    document = _load_document(source_bytes)
    _reject_secret_fields(document)
    normalized_as_of = _normalize_as_of(as_of)
    _validate_migration_boundary(document, as_of=normalized_as_of)

    try:
        legacy = load_config(path, now=normalized_as_of)
    except (ConfigurationError, ValueError) as error:
        raise ApplicationError(
            "LEGACY_CONFIG_INVALID",
            "legacy download configuration failed backend validation",
            False,
            "download_config_migration",
            {"reason": str(error)[:1000]},
        ) from error

    _, after_bytes = _read_safe_source(path)
    if source_bytes != after_bytes:
        raise ApplicationError(
            "LEGACY_CONFIG_SOURCE_CHANGED",
            "legacy download configuration changed while it was being validated",
            False,
            "download_config_migration",
            {"source": str(path)},
        )

    if not legacy.download.resume:
        raise ApplicationError(
            "LEGACY_CONFIG_RESUME_UNREPRESENTABLE",
            "legacy resume=false cannot be represented by the durable v1 job contract",
            False,
            "download_config_migration",
            {"field": "download.resume"},
        )

    try:
        return CreateDownloadRequest(
            exchange_id=legacy.request.exchange_id,
            market_type=legacy.request.market_type,
            symbol=legacy.request.symbol,
            required_data=legacy.request.required_data,
            optional_data=legacy.request.optional_data,
            base_timeframe=legacy.request.base_timeframe,
            derived_timeframes=legacy.request.derived_timeframes,
            start=legacy.request.start,
            end=legacy.request.end,
            full_available_history=legacy.request.full_available_history,
            allow_provisional=legacy.request.allow_provisional,
            storage_format=StorageFormat(legacy.storage.format),
            compression=legacy.storage.compression,
            reject_unresolved_required_gaps=(legacy.validation.reject_unresolved_required_gaps),
            concurrency=legacy.download.concurrency,
            max_records_in_memory=legacy.download.max_records_in_memory,
        )
    except (ValidationError, ValueError) as error:
        details: dict[str, Any] = {"error_type": type(error).__name__}
        if isinstance(error, ValidationError):
            details["errors"] = error.errors(include_input=False)
        raise ApplicationError(
            "LEGACY_CONFIG_UNREPRESENTABLE",
            "validated legacy configuration cannot be represented by CreateDownloadRequest",
            False,
            "download_config_migration",
            details,
        ) from error


def serialize_download_request(request: CreateDownloadRequest) -> str:
    return (
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_download_request(
    output: Path,
    request: CreateDownloadRequest,
) -> Path:
    """Publish deterministic JSON atomically without replacing an existing path."""

    target = _safe_output_target(output)
    payload = serialize_download_request(request).encode("utf-8")
    temporary_fd = -1
    temporary_name: str | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=".market-data-v1-migrate-",
            suffix=".tmp",
            dir=target.parent,
        )
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
            temporary_fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError as error:
            raise ApplicationError(
                "MIGRATION_OUTPUT_EXISTS",
                "migration output path already exists",
                False,
                "download_config_migration",
                {"output": str(target)},
            ) from error
        _fsync_directory(target.parent)
        return target
    except ApplicationError:
        raise
    except OSError as error:
        raise ApplicationError(
            "MIGRATION_OUTPUT_WRITE_FAILED",
            "migration output could not be published safely",
            False,
            "download_config_migration",
            {"output": str(target), "error_type": type(error).__name__},
        ) from error
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            with suppress(OSError):
                Path(temporary_name).unlink(missing_ok=True)


def _read_safe_source(source: Path) -> tuple[Path, bytes]:
    path = source.expanduser().absolute()
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ApplicationError(
            "LEGACY_CONFIG_SOURCE_UNSAFE",
            "legacy configuration must be an existing regular file",
            False,
            "download_config_migration",
            {"source": str(path), "error_type": type(error).__name__},
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ApplicationError(
            "LEGACY_CONFIG_SOURCE_UNSAFE",
            "legacy configuration must be a regular non-symlink file",
            False,
            "download_config_migration",
            {"source": str(path)},
        )
    if metadata.st_size > _MAX_SOURCE_BYTES:
        raise ApplicationError(
            "LEGACY_CONFIG_SOURCE_TOO_LARGE",
            "legacy configuration exceeds the migration size limit",
            False,
            "download_config_migration",
            {
                "source": str(path),
                "observed_bytes": metadata.st_size,
                "limit_bytes": _MAX_SOURCE_BYTES,
            },
        )
    try:
        payload = path.read_bytes()
        payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ApplicationError(
            "LEGACY_CONFIG_SOURCE_UNSAFE",
            "legacy configuration must be readable UTF-8",
            False,
            "download_config_migration",
            {"source": str(path), "error_type": type(error).__name__},
        ) from error
    return path, payload


def _load_document(payload: bytes) -> Mapping[str, Any]:
    try:
        document = yaml.safe_load(payload.decode("utf-8"))
    except yaml.YAMLError as error:
        raise ApplicationError(
            "LEGACY_CONFIG_INVALID",
            "legacy download configuration is not valid YAML",
            False,
            "download_config_migration",
            {"error_type": type(error).__name__},
        ) from error
    if not isinstance(document, Mapping) or any(not isinstance(key, str) for key in document):
        raise ApplicationError(
            "LEGACY_CONFIG_INVALID",
            "legacy download configuration must be a string-keyed mapping",
            False,
            "download_config_migration",
        )
    return document


def _reject_secret_fields(document: Mapping[str, Any]) -> None:
    paths: list[str] = []

    def visit(value: Any, prefix: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = str(key)
                compact = "".join(character for character in name.lower() if character.isalnum())
                current = (*prefix, name)
                if compact in _SECRET_KEYS:
                    paths.append(".".join(current))
                visit(nested, current)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, nested in enumerate(value):
                visit(nested, (*prefix, str(index)))

    visit(document, ())
    if paths:
        raise ApplicationError(
            "LEGACY_CONFIG_SECRET_FIELD_FORBIDDEN",
            "legacy configuration contains credential-like fields that cannot be migrated",
            False,
            "download_config_migration",
            {"field_paths": sorted(set(paths))},
        )


def _normalize_as_of(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ApplicationError(
            "LEGACY_CONFIG_AS_OF_INVALID",
            "migration as-of timestamp must be timezone-aware",
            False,
            "download_config_migration",
        )
    return value.astimezone(UTC)


def _validate_migration_boundary(
    document: Mapping[str, Any],
    *,
    as_of: datetime | None,
) -> None:
    if "request_id" in document:
        raise ApplicationError(
            "LEGACY_CONFIG_REQUEST_ID_UNREPRESENTABLE",
            "explicit legacy request_id cannot be preserved by the durable v1 job identity",
            False,
            "download_config_migration",
            {"field": "request_id"},
        )

    range_document = _optional_mapping(document.get("range"))
    end_value = range_document.get("end", "latest_closed")
    if isinstance(end_value, str) and end_value in _DYNAMIC_END_VALUES and as_of is None:
        raise ApplicationError(
            "LEGACY_CONFIG_DYNAMIC_END_REQUIRES_AS_OF",
            "dynamic latest_closed range requires an explicit migration --as-of timestamp",
            False,
            "download_config_migration",
            {"field": "range.end"},
        )

    storage = _optional_mapping(document.get("storage"))
    if "root" in storage:
        root_value = storage.get("root")
        if isinstance(root_value, str):
            normalized = Path(root_value.strip())
            if normalized.is_absolute() or normalized.parts != ("data",):
                raise ApplicationError(
                    "LEGACY_CONFIG_STORAGE_ROOT_UNREPRESENTABLE",
                    "non-default legacy storage.root cannot be embedded in a v1 request",
                    False,
                    "download_config_migration",
                    {"field": "storage.root"},
                )

    download = _optional_mapping(document.get("download"))
    explicit_runtime = sorted(set(download) & _UNREPRESENTABLE_DOWNLOAD_FIELDS)
    if explicit_runtime:
        raise ApplicationError(
            "LEGACY_CONFIG_RUNTIME_SETTINGS_UNREPRESENTABLE",
            "legacy network runtime settings must be configured separately from the v1 request",
            False,
            "download_config_migration",
            {"fields": [f"download.{field}" for field in explicit_runtime]},
        )

    validation = _optional_mapping(document.get("validation"))
    explicit_validation = sorted(set(validation) & _UNREPRESENTABLE_VALIDATION_FIELDS)
    if explicit_validation:
        raise ApplicationError(
            "LEGACY_CONFIG_VALIDATION_SETTINGS_UNREPRESENTABLE",
            "legacy validation controls are not part of the v1 download request contract",
            False,
            "download_config_migration",
            {"fields": [f"validation.{field}" for field in explicit_validation]},
        )


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_output_target(output: Path) -> Path:
    expanded = output.expanduser().absolute()
    try:
        parent = expanded.parent.resolve(strict=True)
    except OSError as error:
        raise ApplicationError(
            "MIGRATION_OUTPUT_UNSAFE",
            "migration output parent must be an existing directory",
            False,
            "download_config_migration",
            {"output": str(expanded), "error_type": type(error).__name__},
        ) from error
    if parent != expanded.parent or not parent.is_dir() or parent.is_symlink():
        raise ApplicationError(
            "MIGRATION_OUTPUT_UNSAFE",
            "migration output parent must not traverse a symlink",
            False,
            "download_config_migration",
            {"output": str(expanded)},
        )
    target = parent / expanded.name
    if os.path.lexists(target):
        raise ApplicationError(
            "MIGRATION_OUTPUT_EXISTS",
            "migration output path already exists",
            False,
            "download_config_migration",
            {"output": str(target)},
        )
    return target


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
