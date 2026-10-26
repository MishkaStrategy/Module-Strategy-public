from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from decimal import Decimal, InvalidOperation
from collections.abc import Iterator
from typing import Any, Mapping, overload

from .canonical import deterministic_id
from .errors import NonComparableError, ValidationError
from .models import (
    ComparisonStatus,
    MetricDefinitionRecord,
    MetricSnapshot,
    nonblank,
    validate_sha256,
)


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("comparison periods must be ISO-8601 timestamps") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("comparison periods must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical_utc_timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-blank ISO-8601 timestamp")
    parsed = _parse_utc_timestamp(value)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ComparisonContext:
    market: str
    instrument: str
    timeframe: str
    period_start: str
    period_end: str
    dataset_hashes: tuple[str, ...]
    split_id: str
    execution_model: str
    commission_model: str
    slippage_model: str
    funding_model: str
    risk_setting: str
    metric_definition_version: str
    strategy_scope: str
    currency: str
    compounding_mode: str

    def __post_init__(self) -> None:
        for name in (
            "market",
            "instrument",
            "timeframe",
            "split_id",
            "execution_model",
            "commission_model",
            "slippage_model",
            "funding_model",
            "risk_setting",
            "metric_definition_version",
            "strategy_scope",
            "currency",
            "compounding_mode",
        ):
            nonblank(name, getattr(self, name))
        start = _canonical_utc_timestamp(self.period_start, "period_start")
        end = _canonical_utc_timestamp(self.period_end, "period_end")
        if _parse_utc_timestamp(start) >= _parse_utc_timestamp(end):
            raise ValidationError("period_start must be earlier than period_end")
        if not isinstance(self.dataset_hashes, tuple):
            raise ValidationError("dataset_hashes must be a tuple")
        if not self.dataset_hashes:
            raise ValidationError("dataset_hashes cannot be empty")
        for item in self.dataset_hashes:
            validate_sha256("dataset_hash", item)
        if len(set(self.dataset_hashes)) != len(self.dataset_hashes):
            raise ValidationError("dataset_hashes must be unique")
        object.__setattr__(self, "period_start", start)
        object.__setattr__(self, "period_end", end)
        object.__setattr__(self, "dataset_hashes", tuple(sorted(self.dataset_hashes)))

    def as_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {field: getattr(self, field) for field in self.__dataclass_fields__}
        )


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    field: str
    baseline_value: Any
    candidate_value: Any
    blocking: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    status: ComparisonStatus
    issues: tuple[CompatibilityIssue, ...]
    report_id: str

    @property
    def blocking_issues(self) -> tuple[CompatibilityIssue, ...]:
        return tuple(item for item in self.issues if item.blocking)

    @property
    def blocking_mismatches(self) -> tuple[str, ...]:
        return tuple(item.field for item in self.issues if item.blocking)

    @property
    def partial_mismatches(self) -> tuple[str, ...]:
        return tuple(item.field for item in self.issues if not item.blocking)


@dataclass(frozen=True, slots=True)
class MetricDelta:
    metric_name: str
    unit: str
    definition_version: str
    baseline_value: Any | None
    candidate_value: Any | None
    absolute_delta: float | None
    relative_delta: float | None

    @property
    def relative_delta_pct(self) -> Decimal | None:
        return (
            None
            if self.relative_delta is None
            else Decimal(str(self.relative_delta)) * Decimal("100")
        )


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    comparison_id: str
    compatibility: CompatibilityReport
    deltas: tuple[MetricDelta, ...]
    missing_in_baseline: tuple[str, ...]
    missing_in_candidate: tuple[str, ...]
    non_numeric_metrics: tuple[str, ...]

    def __iter__(self) -> Iterator[MetricDelta]:
        return iter(self.deltas)

    def __len__(self) -> int:
        return len(self.deltas)

    @overload
    def __getitem__(self, index: int) -> MetricDelta: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[MetricDelta, ...]: ...

    def __getitem__(self, index: int | slice) -> MetricDelta | tuple[MetricDelta, ...]:
        return self.deltas[index]


def check_compatibility(
    baseline: ComparisonContext,
    candidate: ComparisonContext,
    *,
    partially_comparable_fields: frozenset[str] = frozenset(),
) -> CompatibilityReport:
    if not isinstance(baseline, ComparisonContext) or not isinstance(candidate, ComparisonContext):
        raise ValidationError("baseline and candidate must be ComparisonContext values")
    if not isinstance(partially_comparable_fields, frozenset) or not all(
        isinstance(item, str) for item in partially_comparable_fields
    ):
        raise ValidationError("partially_comparable_fields must be a frozenset of strings")
    unknown = partially_comparable_fields - set(baseline.__dataclass_fields__)
    if unknown:
        raise ValidationError(f"unknown partial compatibility fields: {sorted(unknown)}")
    issues: list[CompatibilityIssue] = []
    for field in baseline.__dataclass_fields__:
        left = getattr(baseline, field)
        right = getattr(candidate, field)
        if left != right:
            issues.append(
                CompatibilityIssue(
                    field=field,
                    baseline_value=left,
                    candidate_value=right,
                    blocking=field not in partially_comparable_fields,
                    reason_code="COMPARISON_CONTEXT_MISMATCH",
                )
            )
    if any(item.blocking for item in issues):
        status = ComparisonStatus.NON_COMPARABLE_RESULTS
    elif issues:
        status = ComparisonStatus.PARTIALLY_COMPARABLE
    else:
        status = ComparisonStatus.COMPARABLE
    payload = {
        "baseline": baseline,
        "candidate": candidate,
        "issues": issues,
        "status": status,
    }
    return CompatibilityReport(
        status=status,
        issues=tuple(issues),
        report_id=deterministic_id("compat", payload, digest_chars=48),
    )


def _numeric(value: Any, metric_name: str) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"metric {metric_name} cannot be converted to Decimal") from exc
    if not result.is_finite():
        raise ValidationError(f"metric {metric_name} must be finite")
    return result


def compare_metrics(
    baseline: tuple[MetricSnapshot, ...],
    candidate: tuple[MetricSnapshot, ...],
    compatibility: CompatibilityReport,
) -> ComparisonResult:
    if not isinstance(baseline, tuple) or not all(
        isinstance(item, MetricSnapshot) for item in baseline
    ):
        raise ValidationError("baseline metrics must be a tuple of MetricSnapshot")
    if not isinstance(candidate, tuple) or not all(
        isinstance(item, MetricSnapshot) for item in candidate
    ):
        raise ValidationError("candidate metrics must be a tuple of MetricSnapshot")
    if not isinstance(compatibility, CompatibilityReport):
        raise ValidationError("compatibility must be a CompatibilityReport")
    if compatibility.status is ComparisonStatus.NON_COMPARABLE_RESULTS:
        raise NonComparableError("results failed the compatibility gate")
    baseline_map = {item.metric_name: item for item in baseline}
    candidate_map = {item.metric_name: item for item in candidate}
    if len(baseline_map) != len(baseline) or len(candidate_map) != len(candidate):
        raise ValidationError("metric names must be unique within each comparison set")
    missing_left = tuple(sorted(set(candidate_map) - set(baseline_map)))
    missing_right = tuple(sorted(set(baseline_map) - set(candidate_map)))
    deltas: list[MetricDelta] = []
    non_numeric: list[str] = []
    for name in sorted(set(baseline_map) | set(candidate_map)):
        left = baseline_map.get(name)
        right = candidate_map.get(name)
        reference = left if left is not None else right
        assert reference is not None
        if left is None or right is None:
            deltas.append(
                MetricDelta(
                    metric_name=name,
                    unit=reference.unit,
                    definition_version=reference.definition_version,
                    baseline_value=None if left is None else left.value,
                    candidate_value=None if right is None else right.value,
                    absolute_delta=None,
                    relative_delta=None,
                )
            )
            continue
        if left.definition_version != right.definition_version:
            raise NonComparableError(f"metric definition mismatch for {name}")
        if left.unit != right.unit:
            raise NonComparableError(f"metric unit mismatch for {name}")
        for field in ("split", "period", "scenario"):
            if getattr(left, field) != getattr(right, field):
                raise NonComparableError(f"metric {field} mismatch for {name}")
        left_value = _numeric(left.value, name)
        right_value = _numeric(right.value, name)
        if left_value is None or right_value is None:
            non_numeric.append(name)
            absolute = None
            relative = None
        else:
            absolute_decimal = right_value - left_value
            relative_decimal = None if left_value == 0 else absolute_decimal / abs(left_value)
            absolute = float(absolute_decimal)
            relative = None if relative_decimal is None else float(relative_decimal)
        deltas.append(
            MetricDelta(
                metric_name=name,
                unit=left.unit,
                definition_version=left.definition_version,
                baseline_value=left.value,
                candidate_value=right.value,
                absolute_delta=absolute,
                relative_delta=relative,
            )
        )
    payload = {
        "compatibility": compatibility.report_id,
        "deltas": deltas,
        "missing_in_baseline": missing_left,
        "missing_in_candidate": missing_right,
        "non_numeric": tuple(non_numeric),
    }
    return ComparisonResult(
        comparison_id=deterministic_id("comparison", payload, digest_chars=48),
        compatibility=compatibility,
        deltas=tuple(deltas),
        missing_in_baseline=missing_left,
        missing_in_candidate=missing_right,
        non_numeric_metrics=tuple(non_numeric),
    )


def _definition_map(
    definitions: tuple[MetricDefinitionRecord, ...],
    *,
    side: str,
) -> dict[str, MetricDefinitionRecord]:
    if not isinstance(definitions, tuple) or not all(
        isinstance(item, MetricDefinitionRecord) for item in definitions
    ):
        raise ValidationError(
            f"{side} metric definitions must be a tuple of MetricDefinitionRecord"
        )
    result: dict[str, MetricDefinitionRecord] = {}
    names: set[tuple[str, str]] = set()
    for definition in definitions:
        if definition.definition_id in result:
            raise ValidationError(f"{side} metric definition IDs must be unique")
        identity = (definition.metric_name, definition.definition_version)
        if identity in names:
            raise ValidationError(f"{side} metric name/version definitions must be unique")
        result[definition.definition_id] = definition
        names.add(identity)
    return result


def compare_metrics_with_definitions(
    baseline: tuple[MetricSnapshot, ...],
    candidate: tuple[MetricSnapshot, ...],
    compatibility: CompatibilityReport,
    *,
    baseline_definitions: tuple[MetricDefinitionRecord, ...],
    candidate_definitions: tuple[MetricDefinitionRecord, ...],
) -> ComparisonResult:
    """Compare snapshots only after exact immutable definition verification.

    Formula strings are evidence and are never executed. Exact content-hash
    equality prevents two registries from reusing a name/version for different
    definitions.
    """

    baseline_map = _definition_map(baseline_definitions, side="baseline")
    candidate_map = _definition_map(candidate_definitions, side="candidate")
    for side, metrics, definitions in (
        ("baseline", baseline, baseline_map),
        ("candidate", candidate, candidate_map),
    ):
        for metric in metrics:
            definition = definitions.get(metric.definition_id)
            if definition is None:
                raise NonComparableError(
                    f"{side} metric {metric.metric_name} has no immutable definition"
                )
            if metric.unit != definition.unit:
                raise NonComparableError(
                    f"{side} metric {metric.metric_name} unit disagrees with definition"
                )
            if metric.precision != definition.precision:
                raise NonComparableError(
                    f"{side} metric {metric.metric_name} precision disagrees with definition"
                )
            if metric.producer != definition.producer:
                raise NonComparableError(
                    f"{side} metric {metric.metric_name} producer disagrees with definition"
                )
    baseline_by_identity = {
        (item.metric_name, item.definition_version): item for item in baseline_definitions
    }
    candidate_by_identity = {
        (item.metric_name, item.definition_version): item for item in candidate_definitions
    }
    for identity in sorted(set(baseline_by_identity) & set(candidate_by_identity)):
        left = baseline_by_identity[identity]
        right = candidate_by_identity[identity]
        if left.content_hash != right.content_hash:
            raise NonComparableError(f"metric definition content mismatch for {identity[0]}")
    return compare_metrics(baseline, candidate, compatibility)
