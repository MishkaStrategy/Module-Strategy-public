from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    DataTypeValidationReport,
    DataValidationReport,
    DuplicateDescriptor,
    ExchangeReference,
    GapDescriptor,
    InstrumentReference,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
    ValidationIssue,
)
from .enums import (
    DataType,
    GapReason,
    MarketType,
    Severity,
    SourceKind,
    ValidationStatus,
)
from .errors import StorageError
from .integrity import RegisteredPathError, resolve_registered_path


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be an object with string keys",
            False,
            "manifest",
        )
    return value


def _object(
    value: object,
    name: str,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> Mapping[str, Any]:
    data = _mapping(value, name)
    unknown = sorted(set(data) - allowed)
    missing = sorted(required - set(data))
    if unknown or missing:
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} has unknown or missing fields",
            False,
            "manifest",
            {"unknown": unknown, "missing": missing},
        )
    return data


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list | tuple):
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be an array",
            False,
            "manifest",
        )
    return tuple(value)


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be a non-empty string",
            False,
            "manifest",
        )
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be an integer",
            False,
            "manifest",
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be a boolean",
            False,
            "manifest",
        )
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    return tuple(
        _string(item, f"{name}[{index}]") for index, item in enumerate(_sequence(value, name))
    )


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} must be an ISO-8601 string",
            False,
            "manifest",
        )
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            f"{name} is not valid ISO-8601",
            False,
            "manifest",
        ) from exc


def _optional_time(value: object, name: str) -> datetime | None:
    return None if value is None else _time(value, name)


def _issue(value: object) -> ValidationIssue:
    data = _object(
        value,
        "validation issue",
        allowed=frozenset({"code", "severity", "message", "start", "end", "evidence"}),
        required=frozenset({"code", "severity", "message"}),
    )
    return ValidationIssue(
        code=_string(data["code"], "issue.code"),
        severity=Severity(_string(data["severity"], "issue.severity")),
        message=_string(data["message"], "issue.message"),
        start=_optional_time(data.get("start"), "issue.start"),
        end=_optional_time(data.get("end"), "issue.end"),
        evidence=dict(_mapping(data.get("evidence", {}), "issue.evidence")),
    )


def _gap(value: object) -> GapDescriptor:
    data = _object(
        value,
        "gap",
        allowed=frozenset({"start", "end", "timeframe", "missing_intervals", "reason", "evidence"}),
        required=frozenset({"start", "end", "timeframe", "missing_intervals"}),
    )
    return GapDescriptor(
        start=_time(data["start"], "gap.start"),
        end=_time(data["end"], "gap.end"),
        timeframe=_string(data["timeframe"], "gap.timeframe"),
        missing_intervals=_integer(data["missing_intervals"], "gap.missing_intervals"),
        reason=GapReason(_string(data.get("reason", GapReason.UNKNOWN.value), "gap.reason")),
        evidence=_strings(data.get("evidence", []), "gap.evidence"),
    )


def _duplicate(value: object) -> DuplicateDescriptor:
    data = _object(
        value,
        "duplicate",
        allowed=frozenset({"timestamp", "exact", "source_partition_ids", "evidence_hashes"}),
        required=frozenset({"timestamp", "exact"}),
    )
    return DuplicateDescriptor(
        timestamp=_time(data["timestamp"], "duplicate.timestamp"),
        exact=_boolean(data["exact"], "duplicate.exact"),
        source_partition_ids=_strings(
            data.get("source_partition_ids", []),
            "duplicate.source_partition_ids",
        ),
        evidence_hashes=_strings(
            data.get("evidence_hashes", []),
            "duplicate.evidence_hashes",
        ),
    )


def _validation(value: object) -> DataValidationReport:
    data = _object(
        value,
        "validation report",
        allowed=frozenset(
            {
                "status",
                "issues",
                "gaps",
                "duplicates",
                "rows_checked",
                "created_at",
                "report_version",
            }
        ),
        required=frozenset({"status", "rows_checked", "created_at"}),
    )
    return DataValidationReport(
        status=ValidationStatus(_string(data["status"], "validation.status")),
        issues=tuple(_issue(item) for item in _sequence(data.get("issues", []), "issues")),
        gaps=tuple(_gap(item) for item in _sequence(data.get("gaps", []), "gaps")),
        duplicates=tuple(
            _duplicate(item) for item in _sequence(data.get("duplicates", []), "duplicates")
        ),
        rows_checked=_integer(data["rows_checked"], "validation.rows_checked"),
        created_at=_time(data["created_at"], "validation.created_at"),
        report_version=_integer(
            data.get("report_version", 1),
            "validation.report_version",
        ),
    )


def _instrument(value: object) -> InstrumentReference:
    data = _object(
        value,
        "instrument",
        allowed=frozenset({"exchange_id", "market_type", "symbol", "canonical_id"}),
        required=frozenset({"exchange_id", "market_type", "symbol", "canonical_id"}),
    )
    return InstrumentReference(
        exchange_id=_string(data["exchange_id"], "instrument.exchange_id"),
        market_type=MarketType(_string(data["market_type"], "instrument.market_type")),
        symbol=_string(data["symbol"], "instrument.symbol"),
        canonical_id=_string(data["canonical_id"], "instrument.canonical_id"),
    )


def _partition(value: object) -> NormalizedPartitionDescriptor:
    data = _object(
        value,
        "partition",
        allowed=frozenset(
            {
                "partition_id",
                "data_type",
                "timeframe",
                "storage_uri",
                "checksum_sha256",
                "row_count",
                "start",
                "end",
                "schema_version",
                "source_artifact_ids",
            }
        ),
        required=frozenset(
            {
                "partition_id",
                "data_type",
                "timeframe",
                "storage_uri",
                "checksum_sha256",
                "row_count",
                "start",
                "end",
                "schema_version",
                "source_artifact_ids",
            }
        ),
    )
    timeframe = data.get("timeframe")
    return NormalizedPartitionDescriptor(
        partition_id=_string(data["partition_id"], "partition.partition_id"),
        data_type=DataType(_string(data["data_type"], "partition.data_type")),
        timeframe=(_string(timeframe, "partition.timeframe") if timeframe is not None else None),
        storage_uri=_string(data["storage_uri"], "partition.storage_uri"),
        checksum_sha256=_string(
            data["checksum_sha256"],
            "partition.checksum_sha256",
        ),
        row_count=_integer(data["row_count"], "partition.row_count"),
        start=_time(data["start"], "partition.start"),
        end=_time(data["end"], "partition.end"),
        schema_version=_integer(
            data["schema_version"],
            "partition.schema_version",
        ),
        source_artifact_ids=_strings(
            data.get("source_artifact_ids", []),
            "partition.source_artifact_ids",
        ),
    )


def _artifact(value: object) -> RawArtifactDescriptor:
    data = _object(
        value,
        "raw artifact",
        allowed=frozenset(
            {
                "artifact_id",
                "source",
                "source_uri",
                "retrieved_at",
                "checksum_sha256",
                "byte_size",
                "record_count",
                "start",
                "end",
                "adapter_version",
                "response_metadata",
            }
        ),
        required=frozenset(
            {
                "artifact_id",
                "source",
                "source_uri",
                "retrieved_at",
                "checksum_sha256",
                "byte_size",
                "record_count",
                "start",
                "end",
                "adapter_version",
            }
        ),
    )
    return RawArtifactDescriptor(
        artifact_id=_string(data["artifact_id"], "artifact.artifact_id"),
        source=SourceKind(_string(data["source"], "artifact.source")),
        source_uri=_string(data["source_uri"], "artifact.source_uri"),
        retrieved_at=_time(data["retrieved_at"], "artifact.retrieved_at"),
        checksum_sha256=_string(
            data["checksum_sha256"],
            "artifact.checksum_sha256",
        ),
        byte_size=_integer(data["byte_size"], "artifact.byte_size"),
        record_count=(
            _integer(data["record_count"], "artifact.record_count")
            if data.get("record_count") is not None
            else None
        ),
        start=_time(data["start"], "artifact.start"),
        end=_time(data["end"], "artifact.end"),
        adapter_version=_string(
            data["adapter_version"],
            "artifact.adapter_version",
        ),
        response_metadata=dict(
            _mapping(data.get("response_metadata", {}), "artifact.response_metadata")
        ),
    )


def manifest_from_dict(payload: Mapping[str, Any]) -> DatasetManifest:
    payload = _object(
        payload,
        "manifest",
        allowed=frozenset(
            {
                "dataset_id",
                "version",
                "exchange",
                "market_type",
                "instrument",
                "data_types",
                "timeframes",
                "start",
                "end",
                "partitions",
                "source_artifacts",
                "validation",
                "normalization_version",
                "validation_policy_version",
                "schema_version",
                "lineage",
                "created_at",
                "data_type_reports",
                "manifest_version",
            }
        ),
        required=frozenset(
            {
                "dataset_id",
                "version",
                "exchange",
                "market_type",
                "instrument",
                "data_types",
                "timeframes",
                "start",
                "end",
                "partitions",
                "source_artifacts",
                "validation",
                "normalization_version",
                "validation_policy_version",
                "schema_version",
                "lineage",
                "created_at",
            }
        ),
    )
    exchange = _object(
        payload["exchange"],
        "exchange",
        allowed=frozenset({"id", "adapter_version"}),
        required=frozenset({"id", "adapter_version"}),
    )
    reports: list[DataTypeValidationReport] = []
    for item in _sequence(payload.get("data_type_reports", []), "data_type_reports"):
        data = _object(
            item,
            "data type validation report",
            allowed=frozenset({"data_type", "timeframe", "report"}),
            required=frozenset({"data_type", "timeframe", "report"}),
        )
        timeframe = data.get("timeframe")
        reports.append(
            DataTypeValidationReport(
                data_type=DataType(_string(data["data_type"], "data_type_report.data_type")),
                timeframe=(
                    _string(timeframe, "data_type_report.timeframe")
                    if timeframe is not None
                    else None
                ),
                report=_validation(data["report"]),
            )
        )
    return DatasetManifest(
        dataset_id=_string(payload["dataset_id"], "manifest.dataset_id"),
        version=_integer(payload["version"], "manifest.version"),
        exchange=ExchangeReference(
            id=_string(exchange["id"], "exchange.id"),
            adapter_version=_string(
                exchange["adapter_version"],
                "exchange.adapter_version",
            ),
        ),
        market_type=MarketType(_string(payload["market_type"], "manifest.market_type")),
        instrument=_instrument(payload["instrument"]),
        data_types=tuple(
            DataType(_string(item, f"data_types[{index}]"))
            for index, item in enumerate(_sequence(payload["data_types"], "data_types"))
        ),
        timeframes=_strings(payload["timeframes"], "timeframes"),
        start=_time(payload["start"], "manifest.start"),
        end=_time(payload["end"], "manifest.end"),
        partitions=tuple(
            _partition(item) for item in _sequence(payload["partitions"], "partitions")
        ),
        source_artifacts=tuple(
            _artifact(item) for item in _sequence(payload["source_artifacts"], "source_artifacts")
        ),
        validation=_validation(payload["validation"]),
        normalization_version=_string(
            payload["normalization_version"],
            "manifest.normalization_version",
        ),
        validation_policy_version=_string(
            payload["validation_policy_version"],
            "manifest.validation_policy_version",
        ),
        schema_version=_integer(
            payload["schema_version"],
            "manifest.schema_version",
        ),
        lineage=_strings(payload["lineage"], "lineage"),
        created_at=_time(payload["created_at"], "manifest.created_at"),
        data_type_reports=tuple(reports),
        manifest_version=_integer(
            payload.get("manifest_version", 2),
            "manifest.manifest_version",
        ),
    )


def load_manifest(storage_root: Path, descriptor: DatasetDescriptor) -> DatasetManifest:
    root = storage_root.expanduser().resolve()
    try:
        path = resolve_registered_path(root, descriptor.manifest_uri, kind="manifest")
    except RegisteredPathError as exc:
        raise StorageError(
            "MANIFEST_PATH_ESCAPE",
            "descriptor manifest path is unsafe",
            False,
            "manifest",
        ) from exc
    if not path.is_file():
        raise StorageError(
            "MANIFEST_MISSING",
            "registered manifest file is missing",
            False,
            "manifest",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(
            "MANIFEST_DECODE_FAILED",
            "registered manifest cannot be decoded",
            False,
            "manifest",
        ) from exc
    try:
        manifest = manifest_from_dict(_mapping(payload, "manifest"))
    except StorageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError(
            "MANIFEST_SCHEMA_INVALID",
            "registered manifest does not satisfy the strict schema",
            False,
            "manifest",
            {"error_type": type(exc).__name__},
        ) from exc
    if manifest.dataset_id != descriptor.dataset_id or manifest.version != descriptor.version:
        raise StorageError(
            "MANIFEST_IDENTITY_MISMATCH",
            "registered manifest identity differs from descriptor",
            False,
            "manifest",
        )
    return manifest
