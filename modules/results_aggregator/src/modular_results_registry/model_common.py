from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .canonical import deep_freeze
from .errors import ValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,191}$")
_SCHEMA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ArtifactStatus(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"
    REGISTERED = "REGISTERED"
    DUPLICATE = "DUPLICATE"
    CONFLICTED = "CONFLICTED"
    CORRUPTED = "CORRUPTED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class ExperimentStatus(StrEnum):
    CREATED = "CREATED"
    INCOMPLETE = "INCOMPLETE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    CONTAMINATED = "CONTAMINATED"
    INVALID = "INVALID"
    ARCHIVED = "ARCHIVED"
    SUPERSEDED = "SUPERSEDED"


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INVALID = "INVALID"


class CandidateRole(StrEnum):
    BASELINE = "BASELINE"
    CANDIDATE = "CANDIDATE"
    CONTROL = "CONTROL"
    ABLATION = "ABLATION"
    REFERENCE = "REFERENCE"
    FINALIST = "FINALIST"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    CONTAMINATED = "CONTAMINATED"
    INCOMPATIBLE = "INCOMPATIBLE"


class CompletenessStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    BLOCKED = "BLOCKED"
    CONTAMINATED = "CONTAMINATED"
    INCOMPATIBLE = "INCOMPATIBLE"


class ComparisonStatus(StrEnum):
    COMPARABLE = "COMPARABLE"
    PARTIALLY_COMPARABLE = "PARTIALLY_COMPARABLE"
    NON_COMPARABLE_RESULTS = "NON_COMPARABLE_RESULTS"


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


class MissingMetricPolicy(StrEnum):
    EXCLUDE = "EXCLUDE"
    ERROR = "ERROR"


class FindingCode(StrEnum):
    PERIOD_CONCENTRATION = "PERIOD_CONCENTRATION"
    INSTRUMENT_CONCENTRATION = "INSTRUMENT_CONCENTRATION"
    SIDE_CONCENTRATION = "SIDE_CONCENTRATION"
    MODULE_CONCENTRATION = "MODULE_CONCENTRATION"
    REGIME_CONCENTRATION = "REGIME_CONCENTRATION"
    TOP_TRADE_DEPENDENCE = "TOP_TRADE_DEPENDENCE"
    PARAMETER_INSTABILITY = "PARAMETER_INSTABILITY"
    COST_SENSITIVITY = "COST_SENSITIVITY"
    OOS_DEGRADATION = "OOS_DEGRADATION"
    WALK_FORWARD_INSTABILITY = "WALK_FORWARD_INSTABILITY"
    SEED_INSTABILITY = "SEED_INSTABILITY"
    INSUFFICIENT_ROBUSTNESS = "INSUFFICIENT_ROBUSTNESS"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    TEST_CONTAMINATION = "TEST_CONTAMINATION"
    IDENTIFIER_CONTENT_CONFLICT = "IDENTIFIER_CONTENT_CONFLICT"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    NON_RELEASE_REPRODUCIBILITY = "NON_RELEASE_REPRODUCIBILITY"


def validate_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(f"{name} has invalid format")


def validate_schema(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SCHEMA_RE.fullmatch(value):
        raise ValidationError(f"{name} has invalid format")


def validate_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{name} must be a lowercase SHA-256 hex digest")


def validate_commit_sha(value: str) -> None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValidationError("commit_sha has invalid format")


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def nonblank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be non-blank")


def integer_value(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    return value


def nonnegative_integer(name: str, value: object) -> int:
    result = integer_value(name, value)
    if result < 0:
        raise ValidationError(f"{name} cannot be negative")
    return result


def finite_nonnegative_number(name: str, value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be numeric")
    if not math.isfinite(value) or value < 0:
        raise ValidationError(f"{name} must be finite and non-negative")
    return value


def finite_metric(value: int | float | str | bool | None) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("metric value must be finite")
        return
    raise ValidationError("metric value has unsupported type")


def safe_relative_path(value: str) -> None:
    if not value or value.startswith(("/", "\\")):
        raise ValidationError("artifact path must be a safe relative path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError("artifact path must be normalized and traversal-free")
    if ":" in parts[0]:
        raise ValidationError("artifact path cannot contain a drive prefix")


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("value must be a mapping")
    return deep_freeze(value)


def _require_enum(name: str, value: object, enum_type: type[StrEnum]) -> None:
    if not isinstance(value, enum_type):
        raise ValidationError(f"{name} must be a {enum_type.__name__}")


def _unique(name: str, values: tuple[str, ...], *, allow_empty: bool = True) -> None:
    if not isinstance(values, tuple):
        raise ValidationError(f"{name} must be a tuple")
    if not allow_empty and not values:
        raise ValidationError(f"{name} must be non-empty")
    if len(set(values)) != len(values):
        raise ValidationError(f"{name} must contain unique values")
