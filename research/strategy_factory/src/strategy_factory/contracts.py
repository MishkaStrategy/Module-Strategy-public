"""Versioned, serializable Strategy Factory contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256, freeze
from .errors import ContractError

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")


def _id(value: str, name: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ContractError(f"{name} must be a portable identifier")
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be non-empty")
    return value.strip()


def _sha(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ContractError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _git_sha(value: str, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise ContractError(f"{name} must be a lowercase 40- or 64-character Git object ID")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(value: int, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ContractError(f"{name} must be a {qualifier} integer")
    return value


def _finite_decimal(value: Decimal, name: str, *, minimum: Decimal | None = None) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractError(f"{name} must be a finite Decimal")
    if minimum is not None and value < minimum:
        raise ContractError(f"{name} must be >= {minimum}")
    return value


def _numeric_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ContractError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ContractError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ContractError(f"{name} must be finite")
    return result


def _validate_parameter_value(value: Any, schema: Mapping[str, Any], name: str) -> None:
    declared_type = schema.get("type")
    if declared_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ContractError(f"{name} must be an integer")
    elif declared_type == "number":
        _numeric_decimal(value, name)
    elif declared_type == "boolean":
        if not isinstance(value, bool):
            raise ContractError(f"{name} must be boolean")
    elif declared_type == "string":
        if not isinstance(value, str):
            raise ContractError(f"{name} must be a string")
    elif declared_type == "array":
        if not isinstance(value, (list, tuple)):
            raise ContractError(f"{name} must be an array")
    elif declared_type == "object":
        if not isinstance(value, Mapping):
            raise ContractError(f"{name} must be an object")
    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        raise ContractError(f"{name} is outside the declared enum")
    if "minimum" in schema:
        if _numeric_decimal(value, name) < _numeric_decimal(schema["minimum"], f"{name}.minimum"):
            raise ContractError(f"{name} is below the declared minimum")
    if "maximum" in schema:
        if _numeric_decimal(value, name) > _numeric_decimal(schema["maximum"], f"{name}.maximum"):
            raise ContractError(f"{name} is above the declared maximum")


def _validate_parameter_search_hint(
    parameter: str,
    schema: Mapping[str, Any],
    hint: Mapping[str, Any],
) -> None:
    steps = hint.get("factory_steps")
    if steps is not None and (
        not isinstance(steps, int) or isinstance(steps, bool) or not 2 <= steps <= 21
    ):
        raise ContractError(f"parameter search hint {parameter}.factory_steps must be in [2, 21]")
    if "minimum" in hint:
        if "minimum" in schema and _numeric_decimal(
            hint["minimum"], f"{parameter}.minimum"
        ) < _numeric_decimal(schema["minimum"], f"{parameter}.schema_minimum"):
            raise ContractError(f"parameter search hint {parameter} widens the minimum")
    if "maximum" in hint:
        if "maximum" in schema and _numeric_decimal(
            hint["maximum"], f"{parameter}.maximum"
        ) > _numeric_decimal(schema["maximum"], f"{parameter}.schema_maximum"):
            raise ContractError(f"parameter search hint {parameter} widens the maximum")
    lower = hint.get("minimum", schema.get("minimum"))
    upper = hint.get("maximum", schema.get("maximum"))
    if (
        lower is not None
        and upper is not None
        and _numeric_decimal(lower, f"{parameter}.minimum")
        > _numeric_decimal(upper, f"{parameter}.maximum")
    ):
        raise ContractError(f"parameter search hint {parameter} has reversed bounds")
    values = hint.get("values")
    if values is not None:
        if not isinstance(values, (list, tuple)) or not values:
            raise ContractError(f"parameter search hint {parameter}.values must be non-empty")
        fingerprints = [canonical_sha256(value) for value in values]
        if len(fingerprints) != len(set(fingerprints)):
            raise ContractError(f"parameter search hint {parameter}.values contains duplicates")
        effective_schema = {**dict(schema)}
        if lower is not None:
            effective_schema["minimum"] = lower
        if upper is not None:
            effective_schema["maximum"] = upper
        for value in values:
            _validate_parameter_value(value, effective_schema, f"{parameter}.search_value")
    elif lower is None or upper is None:
        enum_values = schema.get("enum")
        if enum_values is None:
            raise ContractError(
                f"parameter search hint {parameter} requires values, enum, or bounded range"
            )


class FactoryMode(StrEnum):
    CREATE = "create"
    ADAPT = "adapt"
    IMPROVE = "improve"
    VALIDATE = "validate"


@dataclass(frozen=True, slots=True)
class AdaptationPolicy:
    """Bounded parameter-only transfer policy for an existing strategy package."""

    allow_parameter_adaptation: bool = True
    allowed_parameter_names: frozenset[str] = frozenset()
    neighborhood_points: int = 3
    require_source_baseline_control: bool = True
    allow_direction_reduction: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "allow_parameter_adaptation",
            "require_source_baseline_control",
            "allow_direction_reduction",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} must be boolean")
        if (
            isinstance(self.neighborhood_points, bool)
            or not isinstance(self.neighborhood_points, int)
            or not 1 <= self.neighborhood_points <= 21
        ):
            raise ContractError("adaptation neighborhood_points must be in [1, 21]")
        for parameter in self.allowed_parameter_names:
            _id(parameter, "adaptation allowed parameter")
            if "." not in parameter:
                raise ContractError("adaptation allowed parameters must be runtime-qualified")
        if self.schema_version != 1:
            raise ContractError("unsupported AdaptationPolicy schema_version")


_IMPROVEMENT_MUTATIONS = frozenset(
    {
        "parameters",
        "context_filters",
        "entry",
        "protection",
        "target",
        "position_management",
        "portfolio_constraints",
        "directions",
    }
)


@dataclass(frozen=True, slots=True)
class ImprovementPolicy:
    """Explicit mutation boundary for improving an existing strategy package."""

    allowed_mutations: frozenset[str] = frozenset({"parameters"})
    allowed_parameter_names: frozenset[str] = frozenset()
    neighborhood_points: int = 3
    maximum_runtime_module_changes: int = 0
    maximum_architectures: int = 8
    maximum_complexity_increase: Decimal = Decimal("0")
    require_baseline_control: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        unknown = sorted(self.allowed_mutations - _IMPROVEMENT_MUTATIONS)
        if unknown:
            raise ContractError("unsupported improvement mutations: " + ", ".join(unknown))
        if not self.allowed_mutations:
            raise ContractError("improvement policy requires at least one mutation type")
        for parameter in self.allowed_parameter_names:
            _id(parameter, "improvement allowed parameter")
            if "." not in parameter:
                raise ContractError("improvement allowed parameters must be runtime-qualified")
        if (
            isinstance(self.neighborhood_points, bool)
            or not isinstance(self.neighborhood_points, int)
            or not 1 <= self.neighborhood_points <= 21
        ):
            raise ContractError("improvement neighborhood_points must be in [1, 21]")
        _positive_int(
            self.maximum_runtime_module_changes,
            "maximum_runtime_module_changes",
            allow_zero=True,
        )
        _positive_int(self.maximum_architectures, "maximum_architectures")
        increase = _finite_decimal(
            self.maximum_complexity_increase,
            "maximum_complexity_increase",
            minimum=Decimal("0"),
        )
        object.__setattr__(self, "maximum_complexity_increase", increase)
        if not isinstance(self.require_baseline_control, bool):
            raise ContractError("require_baseline_control must be boolean")
        structural = self.allowed_mutations - {"parameters"}
        if structural and self.maximum_runtime_module_changes < 1:
            raise ContractError(
                "structural improvement mutations require a positive module-change limit"
            )
        if self.schema_version != 1:
            raise ContractError("unsupported ImprovementPolicy schema_version")


class FactoryStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DATASET_REJECTED = "DATASET_REJECTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"


class FactoryStage(StrEnum):
    DATA_GATE = "data_gate"
    INSTRUMENT_PROFILE = "instrument_profile"
    REGIME_DISCOVERY = "regime_discovery"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    ARCHITECTURE_GENERATION = "architecture_generation"
    EXPERIMENT_PLANNING = "experiment_planning"
    STRUCTURAL_SCREENING = "structural_screening"
    COARSE_SEARCH = "coarse_search"
    LOCAL_REFINEMENT = "local_refinement"
    WALK_FORWARD = "walk_forward"
    ROBUSTNESS = "robustness"
    HOLDOUT = "holdout"
    SIMPLICITY_SELECTION = "simplicity_selection"
    FINAL_AUDIT = "final_audit"
    PACKAGE = "package"
    PAPER_ADMISSION = "paper_admission"


class DatasetGateStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_WARNINGS = "ACCEPTED_WITH_WARNINGS"
    REJECTED = "REJECTED"


class MarketType(StrEnum):
    SPOT = "spot"
    LINEAR_FUTURES = "linear_futures"
    INVERSE_FUTURES = "inverse_futures"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class ModuleCategory(StrEnum):
    SIGNAL = "signal"
    CONTEXT_FILTER = "context_filter"
    SETUP_ADAPTER = "setup_adapter"
    ENTRY = "entry"
    PROTECTION = "protection"
    TARGET = "target"
    RISK_REQUEST_ADAPTER = "risk_request_adapter"
    RISK = "risk"
    POSITION_MANAGEMENT = "position_management"
    PORTFOLIO_CONSTRAINT = "portfolio_constraint"


class ModuleMaturity(StrEnum):
    EXPERIMENTAL = "experimental"
    RESEARCH = "research"
    PAPER_READY = "paper_ready"
    RELEASED = "released"
    DEPRECATED = "deprecated"


class AdmissionStatus(StrEnum):
    APPROVED_FOR_PAPER = "APPROVED_FOR_PAPER"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class DecisionAction(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    PRUNE = "prune"
    PROMOTE = "promote"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    BLOCK = "block"
    SKIP = "skip"
    FAIL = "fail"
    EXHAUST = "exhaust"


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "time range start"))
        object.__setattr__(self, "end", _utc(self.end, "time range end"))
        if self.end <= self.start:
            raise ContractError("time range end must be later than start")


@dataclass(frozen=True, slots=True)
class DatasetDescriptor:
    dataset_id: str
    source: str
    symbol: str
    exchange: str
    market_type: MarketType
    base_timeframe: str
    available_timeframes: tuple[str, ...]
    period: TimeRange
    timezone: str
    file_format: str
    uri: str
    sha256: str
    row_count: int
    received_at: datetime
    schema_version: int = 1
    funding_uri: str | None = None
    funding_sha256: str | None = None
    exchange_info_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("dataset_id", "source", "symbol", "exchange", "base_timeframe"):
            _id(getattr(self, name), name)
        if self.timezone != "UTC":
            raise ContractError("dataset timezone must be UTC")
        if self.file_format not in {"csv", "parquet"}:
            raise ContractError("dataset format must be csv or parquet")
        _text(self.uri, "dataset uri")
        _sha(self.sha256, "dataset sha256")
        _positive_int(self.row_count, "dataset row_count")
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if not self.available_timeframes or self.base_timeframe not in self.available_timeframes:
            raise ContractError("available_timeframes must include base_timeframe")
        if len(set(self.available_timeframes)) != len(self.available_timeframes):
            raise ContractError("available_timeframes must be unique")
        if self.funding_uri is not None:
            _text(self.funding_uri, "funding_uri")
            if self.funding_sha256 is None:
                raise ContractError("funding_uri requires funding_sha256")
        if self.funding_sha256 is not None:
            _sha(self.funding_sha256, "funding_sha256")
        if self.exchange_info_sha256 is not None:
            _sha(self.exchange_info_sha256, "exchange_info_sha256")
        if self.schema_version != 1:
            raise ContractError("unsupported DatasetDescriptor schema_version")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class DatasetValidationReport:
    report_id: str
    dataset_id: str
    dataset_fingerprint: str
    status: DatasetGateStatus
    checked_at: datetime
    rows_scanned: int
    duplicates: int
    gaps: int
    out_of_order: int
    invalid_ohlc: int
    invalid_volume: int
    extreme_outliers: int
    computed_sha256: str
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.report_id, "report_id")
        _id(self.dataset_id, "dataset_id")
        _sha(self.dataset_fingerprint, "dataset_fingerprint")
        _sha(self.computed_sha256, "computed_sha256")
        object.__setattr__(self, "checked_at", _utc(self.checked_at, "checked_at"))
        for name in (
            "rows_scanned",
            "duplicates",
            "gaps",
            "out_of_order",
            "invalid_ohlc",
            "invalid_volume",
            "extreme_outliers",
        ):
            _positive_int(getattr(self, name), name, allow_zero=True)
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "evidence", freeze(self.evidence))
        if self.status is DatasetGateStatus.REJECTED and not self.reason_codes:
            raise ContractError("rejected dataset report requires reason_codes")
        if self.schema_version != 1:
            raise ContractError("unsupported DatasetValidationReport schema_version")


@dataclass(frozen=True, slots=True)
class InstrumentProfile:
    profile_id: str
    dataset_fingerprint: str
    symbol: str
    market_type: MarketType
    generated_at: datetime
    history_days: Decimal
    observations: int
    liquidity_metrics: Mapping[str, Any]
    volatility_metrics: Mapping[str, Any]
    return_distribution: Mapping[str, Any]
    trend_metrics: Mapping[str, Any]
    mean_reversion_metrics: Mapping[str, Any]
    seasonality_metrics: Mapping[str, Any]
    tail_risk_metrics: Mapping[str, Any]
    funding_metrics: Mapping[str, Any]
    cost_sensitivity: Mapping[str, Any]
    stability_metrics: Mapping[str, Any]
    warnings: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.profile_id, "profile_id")
        _sha(self.dataset_fingerprint, "dataset_fingerprint")
        _id(self.symbol, "symbol")
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        _finite_decimal(self.history_days, "history_days", minimum=Decimal("0"))
        _positive_int(self.observations, "observations")
        for name in (
            "liquidity_metrics",
            "volatility_metrics",
            "return_distribution",
            "trend_metrics",
            "mean_reversion_metrics",
            "seasonality_metrics",
            "tail_risk_metrics",
            "funding_metrics",
            "cost_sensitivity",
            "stability_metrics",
        ):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if self.schema_version != 1:
            raise ContractError("unsupported InstrumentProfile schema_version")


@dataclass(frozen=True, slots=True)
class MarketRegimeDefinition:
    regime_id: str
    name: str
    description: str
    feature_version: str
    point_in_time: bool
    required_timeframes: tuple[str, ...]
    parameters: Mapping[str, Any]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.regime_id, "regime_id")
        _text(self.name, "regime name")
        _text(self.description, "regime description")
        if not _VERSION_RE.fullmatch(self.feature_version):
            raise ContractError("feature_version is invalid")
        if not isinstance(self.point_in_time, bool) or not self.point_in_time:
            raise ContractError("regime definitions must be point-in-time")
        if len(set(self.required_timeframes)) != len(self.required_timeframes):
            raise ContractError("required_timeframes must be unique")
        object.__setattr__(self, "parameters", freeze(self.parameters))
        if self.schema_version != 1:
            raise ContractError("unsupported MarketRegimeDefinition schema_version")


@dataclass(frozen=True, slots=True)
class ModuleReference:
    module_id: str
    version: str
    category: ModuleCategory
    import_path: str
    api_version: str
    state_schema_version: str
    required_timeframes: tuple[str, ...]
    required_features: tuple[str, ...]
    produced_features: tuple[str, ...]
    required_data: frozenset[str]
    supported_markets: frozenset[MarketType]
    supported_directions: frozenset[Direction]
    deterministic: bool
    test_coverage: Decimal
    maturity: ModuleMaturity
    optional_features: tuple[str, ...] = ()
    fixed_parameters: Mapping[str, Any] = field(default_factory=dict)
    parameter_search_hints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence_links: tuple[str, ...] = ()
    source_commit_sha: str | None = None
    compatibility_tags: frozenset[str] = frozenset()
    conflict_tags: frozenset[str] = frozenset()
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    runtime_component_id: str | None = None
    runtime_component_version: str | None = None
    runtime_role_categories: frozenset[ModuleCategory] = frozenset()
    role_facet: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.module_id, "module_id")
        if not _VERSION_RE.fullmatch(self.version):
            raise ContractError("module version is invalid")
        _text(self.import_path, "import_path")
        _text(self.api_version, "api_version")
        _text(self.state_schema_version, "state_schema_version")
        if not self.supported_markets:
            raise ContractError("module must support at least one market")
        if not self.supported_directions:
            raise ContractError("module must support at least one direction")
        if not isinstance(self.deterministic, bool):
            raise ContractError("deterministic must be bool")
        _finite_decimal(
            self.test_coverage,
            "test_coverage",
            minimum=Decimal("0"),
        )
        if self.test_coverage > Decimal("100"):
            raise ContractError("test_coverage cannot exceed 100")
        for values, name in (
            (self.required_timeframes, "required_timeframes"),
            (self.required_features, "required_features"),
            (self.produced_features, "produced_features"),
            (self.optional_features, "optional_features"),
        ):
            if len(set(values)) != len(values):
                raise ContractError(f"{name} must be unique")
        object.__setattr__(self, "parameter_schema", freeze(self.parameter_schema))
        object.__setattr__(self, "fixed_parameters", freeze(self.fixed_parameters))
        object.__setattr__(self, "parameter_search_hints", freeze(self.parameter_search_hints))
        unknown_fixed = sorted(set(self.fixed_parameters) - set(self.parameter_schema))
        if unknown_fixed:
            raise ContractError(
                "fixed module parameters are absent from parameter_schema: "
                + ", ".join(unknown_fixed)
            )
        unknown_hints = sorted(set(self.parameter_search_hints) - set(self.parameter_schema))
        if unknown_hints:
            raise ContractError(
                "parameter search hints are absent from parameter_schema: "
                + ", ".join(unknown_hints)
            )
        overlap = sorted(set(self.fixed_parameters) & set(self.parameter_search_hints))
        if overlap:
            raise ContractError("fixed parameters cannot also be searchable: " + ", ".join(overlap))
        for name, value in self.fixed_parameters.items():
            schema = self.parameter_schema[name]
            if not isinstance(schema, Mapping):
                raise ContractError(f"parameter schema {name} must be an object")
            _validate_parameter_value(value, schema, f"fixed parameter {name}")
        for name, hint in self.parameter_search_hints.items():
            if not isinstance(hint, Mapping):
                raise ContractError(f"parameter search hint {name} must be an object")
            unknown = sorted(set(hint) - {"values", "minimum", "maximum", "factory_steps"})
            if unknown:
                raise ContractError(
                    f"parameter search hint {name} has unsupported fields: " + ", ".join(unknown)
                )
            schema = self.parameter_schema[name]
            if not isinstance(schema, Mapping):
                raise ContractError(f"parameter schema {name} must be an object")
            _validate_parameter_search_hint(name, schema, hint)
        for link in self.evidence_links:
            _text(link, "module evidence link")
        if self.source_commit_sha is not None:
            _git_sha(self.source_commit_sha, "module source_commit_sha")
        runtime_component_id = self.runtime_component_id or self.module_id
        runtime_component_version = self.runtime_component_version or self.version
        _id(runtime_component_id, "runtime_component_id")
        if not _VERSION_RE.fullmatch(runtime_component_version):
            raise ContractError("runtime component version is invalid")
        object.__setattr__(self, "runtime_component_id", runtime_component_id)
        object.__setattr__(self, "runtime_component_version", runtime_component_version)
        runtime_roles = self.runtime_role_categories or frozenset({self.category})
        if self.category not in runtime_roles:
            raise ContractError("runtime_role_categories must contain the logical category")
        if len(runtime_roles) > 1 and self.role_facet is None:
            raise ContractError("multi-role runtime component requires role_facet")
        if self.role_facet is not None:
            _id(self.role_facet, "role_facet")
        object.__setattr__(self, "runtime_role_categories", frozenset(runtime_roles))
        if self.schema_version != 1:
            raise ContractError("unsupported ModuleReference schema_version")

    @property
    def key(self) -> str:
        return f"{self.module_id}@{self.version}"

    @property
    def runtime_key(self) -> str:
        return f"{self.runtime_component_id}@{self.runtime_component_version}"


@dataclass(frozen=True, slots=True)
class StrategyHypothesis:
    hypothesis_id: str
    description: str
    expected_edge: str
    applicability: tuple[str, ...]
    risks: tuple[str, ...]
    module_candidates: tuple[ModuleReference, ...]
    parameter_ranges: Mapping[str, Any]
    falsification_criteria: Mapping[str, Any]
    estimated_experiments: int
    novelty: Decimal
    origin: Mapping[str, Any]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.hypothesis_id, "hypothesis_id")
        _text(self.description, "hypothesis description")
        _text(self.expected_edge, "expected_edge")
        _positive_int(self.estimated_experiments, "estimated_experiments")
        _finite_decimal(self.novelty, "novelty", minimum=Decimal("0"))
        if self.novelty > Decimal("1"):
            raise ContractError("novelty must be in [0, 1]")
        for name in ("parameter_ranges", "falsification_criteria", "origin"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyHypothesis schema_version")


@dataclass(frozen=True, slots=True)
class StrategyArchitecture:
    architecture_id: str
    market_type: MarketType
    directions: frozenset[Direction]
    signal: ModuleReference
    context_filters: tuple[ModuleReference, ...]
    entry: ModuleReference
    protection: ModuleReference
    target: ModuleReference
    risk: ModuleReference
    setup_adapters: tuple[ModuleReference, ...] = ()
    risk_adapters: tuple[ModuleReference, ...] = ()
    position_management: tuple[ModuleReference, ...] = ()
    portfolio_constraints: tuple[ModuleReference, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    grammar_version: str = "1.0"
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.architecture_id, "architecture_id")
        if not self.directions:
            raise ContractError("strategy architecture requires at least one direction")
        if self.signal.category is not ModuleCategory.SIGNAL:
            raise ContractError("signal reference has the wrong category")
        if any(item.category is not ModuleCategory.SETUP_ADAPTER for item in self.setup_adapters):
            raise ContractError("setup_adapters contains a wrong category")
        if self.entry.category is not ModuleCategory.ENTRY:
            raise ContractError("entry reference has the wrong category")
        if self.protection.category is not ModuleCategory.PROTECTION:
            raise ContractError("protection reference has the wrong category")
        if self.target.category is not ModuleCategory.TARGET:
            raise ContractError("target reference has the wrong category")
        if any(
            item.category is not ModuleCategory.RISK_REQUEST_ADAPTER for item in self.risk_adapters
        ):
            raise ContractError("risk_adapters contains a wrong category")
        if self.risk.category is not ModuleCategory.RISK:
            raise ContractError("risk reference has the wrong category")
        if any(item.category is not ModuleCategory.CONTEXT_FILTER for item in self.context_filters):
            raise ContractError("context_filters contains a wrong category")
        if any(
            item.category is not ModuleCategory.POSITION_MANAGEMENT
            for item in self.position_management
        ):
            raise ContractError("position_management contains a wrong category")
        if any(
            item.category is not ModuleCategory.PORTFOLIO_CONSTRAINT
            for item in self.portfolio_constraints
        ):
            raise ContractError("portfolio_constraints contains a wrong category")
        all_keys = [item.key for item in self.modules]
        if len(all_keys) != len(set(all_keys)):
            raise ContractError("strategy architecture contains duplicate logical module roles")
        self._validate_runtime_components()
        object.__setattr__(self, "parameters", freeze(self.parameters))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyArchitecture schema_version")

    @property
    def modules(self) -> tuple[ModuleReference, ...]:
        return (
            self.signal,
            *self.context_filters,
            *self.setup_adapters,
            self.entry,
            self.protection,
            self.target,
            *self.risk_adapters,
            self.risk,
            *self.position_management,
            *self.portfolio_constraints,
        )

    @property
    def runtime_modules(self) -> tuple[ModuleReference, ...]:
        """Return one representative for every actual runtime component instance."""

        by_runtime: dict[str, ModuleReference] = {}
        for module in self.modules:
            by_runtime.setdefault(module.runtime_key, module)
        return tuple(by_runtime[key] for key in sorted(by_runtime))

    def _validate_runtime_components(self) -> None:
        grouped: dict[str, list[ModuleReference]] = {}
        for module in self.modules:
            grouped.setdefault(module.runtime_key, []).append(module)
        for runtime_key, references in grouped.items():
            if len(references) == 1:
                continue
            categories = frozenset(item.category for item in references)
            if any(categories != item.runtime_role_categories for item in references):
                raise ContractError(
                    f"shared runtime component {runtime_key} has undeclared logical roles"
                )
            contract_fingerprints = {
                canonical_sha256(
                    {
                        "import_path": item.import_path,
                        "api_version": item.api_version,
                        "state_schema_version": item.state_schema_version,
                        "required_timeframes": item.required_timeframes,
                        "required_features": item.required_features,
                        "produced_features": item.produced_features,
                        "optional_features": item.optional_features,
                        "fixed_parameters": item.fixed_parameters,
                        "parameter_search_hints": item.parameter_search_hints,
                        "required_data": item.required_data,
                        "supported_markets": item.supported_markets,
                        "supported_directions": item.supported_directions,
                        "deterministic": item.deterministic,
                        "parameter_schema": item.parameter_schema,
                    }
                )
                for item in references
            }
            if len(contract_fingerprints) != 1:
                raise ContractError(
                    f"shared runtime component {runtime_key} has conflicting contracts"
                )
            facets = [item.role_facet for item in references]
            if None in facets or len(facets) != len(set(facets)):
                raise ContractError(
                    f"shared runtime component {runtime_key} requires unique role facets"
                )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ParameterSpace:
    space_id: str
    architecture_id: str
    dimensions: Mapping[str, tuple[Any, ...]]
    constraints: tuple[Mapping[str, Any], ...] = ()
    maximum_combinations: int = 100_000
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.space_id, "space_id")
        _id(self.architecture_id, "architecture_id")
        if not self.dimensions:
            raise ContractError("parameter space requires dimensions")
        normalized: dict[str, tuple[Any, ...]] = {}
        for name, values in sorted(self.dimensions.items()):
            _id(name, "parameter name")
            if not isinstance(values, tuple) or not values:
                raise ContractError("parameter dimension values must be a non-empty tuple")
            hashes = [canonical_sha256(value) for value in values]
            if len(hashes) != len(set(hashes)):
                raise ContractError(f"parameter dimension {name} contains duplicates")
            normalized[name] = values
        object.__setattr__(self, "dimensions", MappingProxyType(normalized))
        object.__setattr__(self, "constraints", tuple(freeze(item) for item in self.constraints))
        _positive_int(self.maximum_combinations, "maximum_combinations")
        if self.schema_version != 1:
            raise ContractError("unsupported ParameterSpace schema_version")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    maximum_experiments: int
    maximum_architectures: int
    maximum_finalists: int
    maximum_duration_seconds: int
    maximum_memory_bytes: int
    maximum_cpu_workers: int
    maximum_parallel_tasks: int
    stage_experiment_limits: Mapping[str | FactoryStage, int]
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "maximum_experiments",
            "maximum_architectures",
            "maximum_finalists",
            "maximum_duration_seconds",
            "maximum_memory_bytes",
            "maximum_cpu_workers",
            "maximum_parallel_tasks",
        ):
            _positive_int(getattr(self, name), name)
        normalized: dict[str, int] = {}
        for raw_stage, limit in self.stage_experiment_limits.items():
            try:
                stage = (
                    raw_stage if isinstance(raw_stage, FactoryStage) else FactoryStage(raw_stage)
                )
            except (TypeError, ValueError) as exc:
                raise ContractError("invalid stage_experiment_limits key") from exc
            normalized[stage.value] = _positive_int(limit, f"stage limit {stage.value}")
        if sum(normalized.values()) > self.maximum_experiments:
            raise ContractError("stage limits exceed maximum_experiments")
        object.__setattr__(self, "stage_experiment_limits", MappingProxyType(normalized))
        if self.schema_version != 1:
            raise ContractError("unsupported ResourceBudget schema_version")


@dataclass(frozen=True, slots=True)
class StrategyFactoryRequest:
    request_id: str
    mode: FactoryMode
    symbol: str
    exchange: str
    market_type: MarketType
    dataset: DatasetDescriptor
    allowed_timeframes: tuple[str, ...]
    initial_capital: Decimal
    research_risk_fraction: Decimal
    maximum_drawdown_fraction: Decimal
    minimum_trade_count: int
    objectives: Mapping[str, Any]
    budget: ResourceBudget
    random_seed: int
    allowed_strategy_families: frozenset[str]
    forbidden_module_ids: frozenset[str] = frozenset()
    baseline_strategy_id: str | None = None
    source_strategy_id: str | None = None
    adaptation_policy: AdaptationPolicy = field(default_factory=AdaptationPolicy)
    improvement_policy: ImprovementPolicy = field(default_factory=ImprovementPolicy)
    explicit_holdout: TimeRange | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.symbol, "symbol")
        _id(self.exchange, "exchange")
        if self.dataset.symbol != self.symbol:
            raise ContractError("request symbol does not match dataset symbol")
        if self.dataset.market_type is not self.market_type:
            raise ContractError("request market_type does not match dataset")
        if not self.allowed_timeframes:
            raise ContractError("allowed_timeframes cannot be empty")
        if any(item not in self.dataset.available_timeframes for item in self.allowed_timeframes):
            raise ContractError("allowed_timeframes contains unavailable data")
        _finite_decimal(self.initial_capital, "initial_capital", minimum=Decimal("0.00000001"))
        for name in ("research_risk_fraction", "maximum_drawdown_fraction"):
            value = _finite_decimal(getattr(self, name), name, minimum=Decimal("0"))
            if value > Decimal("1"):
                raise ContractError(f"{name} must not exceed 1")
        _positive_int(self.minimum_trade_count, "minimum_trade_count")
        _positive_int(self.random_seed, "random_seed", allow_zero=True)
        if not self.allowed_strategy_families:
            raise ContractError("allowed_strategy_families cannot be empty")
        if self.mode is FactoryMode.ADAPT and self.source_strategy_id is None:
            raise ContractError("adapt mode requires source_strategy_id")
        if (
            self.mode in {FactoryMode.IMPROVE, FactoryMode.VALIDATE}
            and self.baseline_strategy_id is None
        ):
            raise ContractError(f"{self.mode.value} mode requires baseline_strategy_id")
        if self.mode is FactoryMode.VALIDATE and self.explicit_holdout is None:
            raise ContractError("validate mode requires explicit_holdout")
        if not isinstance(self.adaptation_policy, AdaptationPolicy):
            raise ContractError("adaptation_policy must be AdaptationPolicy")
        if not isinstance(self.improvement_policy, ImprovementPolicy):
            raise ContractError("improvement_policy must be ImprovementPolicy")
        object.__setattr__(self, "objectives", freeze(self.objectives))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyFactoryRequest schema_version")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    plan_id: str
    factory_run_id: str
    stage: FactoryStage
    architecture: StrategyArchitecture
    parameter_space: ParameterSpace
    dataset_fingerprint: str
    research_specification: Mapping[str, Any]
    resource_limits: Mapping[str, Any]
    deterministic_seed: int
    manifest_id: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("plan_id", "factory_run_id", "manifest_id"):
            _id(getattr(self, name), name)
        _sha(self.dataset_fingerprint, "dataset_fingerprint")
        if self.parameter_space.architecture_id != self.architecture.architecture_id:
            raise ContractError("parameter space belongs to a different architecture")
        _positive_int(self.deterministic_seed, "deterministic_seed", allow_zero=True)
        object.__setattr__(self, "research_specification", freeze(self.research_specification))
        object.__setattr__(self, "resource_limits", freeze(self.resource_limits))
        if self.schema_version != 1:
            raise ContractError("unsupported ExperimentPlan schema_version")


@dataclass(frozen=True, slots=True)
class ExperimentBatchRequest:
    batch_id: str
    factory_run_id: str
    plans: tuple[ExperimentPlan, ...]
    idempotency_key: str
    created_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.batch_id, "batch_id")
        _id(self.factory_run_id, "factory_run_id")
        _id(self.idempotency_key, "idempotency_key")
        if not self.plans:
            raise ContractError("experiment batch must contain plans")
        if len({item.plan_id for item in self.plans}) != len(self.plans):
            raise ContractError("experiment batch contains duplicate plan IDs")
        if any(item.factory_run_id != self.factory_run_id for item in self.plans):
            raise ContractError("experiment batch mixes factory runs")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported ExperimentBatchRequest schema_version")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ExperimentBatchResult:
    batch_id: str
    factory_run_id: str
    status: str
    completed_plan_ids: tuple[str, ...]
    failed_plan_ids: tuple[str, ...]
    result_references: tuple[str, ...]
    failures: tuple[Mapping[str, Any], ...]
    received_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.batch_id, "batch_id")
        _id(self.factory_run_id, "factory_run_id")
        _text(self.status, "batch status")
        if set(self.completed_plan_ids) & set(self.failed_plan_ids):
            raise ContractError("completed and failed plan sets overlap")
        object.__setattr__(self, "failures", tuple(freeze(item) for item in self.failures))
        object.__setattr__(self, "received_at", _utc(self.received_at, "received_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported ExperimentBatchResult schema_version")


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    evaluation_id: str
    candidate_id: str
    metrics: Mapping[str, Any]
    constraints: Mapping[str, bool]
    pareto_front: int
    instability_flags: tuple[str, ...]
    complexity: Decimal
    evidence_references: tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.evaluation_id, "evaluation_id")
        _id(self.candidate_id, "candidate_id")
        _positive_int(self.pareto_front, "pareto_front", allow_zero=True)
        _finite_decimal(self.complexity, "complexity", minimum=Decimal("0"))
        object.__setattr__(self, "metrics", freeze(self.metrics))
        object.__setattr__(self, "constraints", freeze(self.constraints))
        if self.schema_version != 1:
            raise ContractError("unsupported CandidateEvaluation schema_version")


@dataclass(frozen=True, slots=True)
class CandidateStrategy:
    candidate_id: str
    architecture: StrategyArchitecture
    parameters: Mapping[str, Any]
    configuration_hash: str
    lineage_references: tuple[str, ...]
    evaluation: CandidateEvaluation | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.candidate_id, "candidate_id")
        _sha(self.configuration_hash, "configuration_hash")
        object.__setattr__(self, "parameters", freeze(self.parameters))
        if canonical_sha256(self.parameters) != self.configuration_hash:
            raise ContractError("configuration_hash does not match candidate parameters")
        if self.evaluation is not None and self.evaluation.candidate_id != self.candidate_id:
            raise ContractError("candidate evaluation belongs to a different candidate")
        if self.schema_version != 1:
            raise ContractError("unsupported CandidateStrategy schema_version")


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    report_id: str
    candidate_id: str
    passed: bool
    scenario_results: Mapping[str, Any]
    failure_reasons: tuple[str, ...]
    generated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.report_id, "report_id")
        _id(self.candidate_id, "candidate_id")
        if not isinstance(self.passed, bool):
            raise ContractError("passed must be bool")
        object.__setattr__(self, "scenario_results", freeze(self.scenario_results))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        if not self.passed and not self.failure_reasons:
            raise ContractError("failed robustness report requires failure reasons")
        if self.schema_version != 1:
            raise ContractError("unsupported RobustnessReport schema_version")


@dataclass(frozen=True, slots=True)
class HoldoutReport:
    report_id: str
    candidate_id: str
    period: TimeRange
    dataset_fingerprint: str
    metrics: Mapping[str, Any]
    passed: bool
    opened_once: bool
    generated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.report_id, "report_id")
        _id(self.candidate_id, "candidate_id")
        _sha(self.dataset_fingerprint, "dataset_fingerprint")
        if not isinstance(self.passed, bool) or not isinstance(self.opened_once, bool):
            raise ContractError("holdout booleans are invalid")
        if not self.opened_once:
            raise ContractError("holdout report must prove a one-time opening")
        object.__setattr__(self, "metrics", freeze(self.metrics))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported HoldoutReport schema_version")


@dataclass(frozen=True, slots=True)
class FactoryDecision:
    decision_id: str
    run_id: str
    stage: FactoryStage
    action: DecisionAction
    subject_id: str
    reason_codes: tuple[str, ...]
    evidence: Mapping[str, Any]
    alternatives: tuple[str, ...]
    budget_snapshot: Mapping[str, Any]
    decided_at: datetime
    actor: str = "strategy_factory"
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("decision_id", "run_id", "subject_id", "actor"):
            _id(getattr(self, name), name)
        if not self.reason_codes:
            raise ContractError("factory decision requires reason codes")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        object.__setattr__(self, "evidence", freeze(self.evidence))
        object.__setattr__(self, "budget_snapshot", freeze(self.budget_snapshot))
        object.__setattr__(self, "decided_at", _utc(self.decided_at, "decided_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported FactoryDecision schema_version")


@dataclass(frozen=True, slots=True)
class StrategyAuditDecision:
    audit_id: str
    candidate_id: str
    status: AdmissionStatus
    evidence_package_reference: str
    evidence_hash: str
    reason_codes: tuple[str, ...]
    issued_at: datetime
    auditor_id: str
    contract_version: str
    signature: str
    decision_hash: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("audit_id", "candidate_id", "auditor_id"):
            _id(getattr(self, name), name)
        _text(self.evidence_package_reference, "evidence_package_reference")
        _sha(self.evidence_hash, "evidence_hash")
        _text(self.contract_version, "contract_version")
        _text(self.signature, "signature")
        _sha(self.decision_hash, "decision_hash")
        if not self.reason_codes or any(
            not isinstance(value, str) or not value.strip() for value in self.reason_codes
        ):
            raise ContractError("strategy audit decision requires reason codes")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyAuditDecision schema_version")
        if self.decision_hash != canonical_sha256(self.hash_payload()):
            raise ContractError("strategy audit decision hash mismatch")

    def hash_payload(self) -> Mapping[str, Any]:
        return {
            "audit_id": self.audit_id,
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "evidence_package_reference": self.evidence_package_reference,
            "evidence_hash": self.evidence_hash,
            "reason_codes": self.reason_codes,
            "issued_at": self.issued_at,
            "auditor_id": self.auditor_id,
            "contract_version": self.contract_version,
            "signature": self.signature,
            "schema_version": self.schema_version,
        }

    @classmethod
    def issue(
        cls,
        *,
        audit_id: str,
        candidate_id: str,
        status: AdmissionStatus,
        evidence_package_reference: str,
        evidence_hash: str,
        reason_codes: tuple[str, ...],
        issued_at: datetime,
        auditor_id: str,
        contract_version: str,
        signature: str,
    ) -> StrategyAuditDecision:
        normalized_reasons = tuple(sorted(set(reason_codes)))
        payload = {
            "audit_id": audit_id,
            "candidate_id": candidate_id,
            "status": status.value,
            "evidence_package_reference": evidence_package_reference,
            "evidence_hash": evidence_hash,
            "reason_codes": normalized_reasons,
            "issued_at": _utc(issued_at, "issued_at"),
            "auditor_id": auditor_id,
            "contract_version": contract_version,
            "signature": signature,
            "schema_version": 1,
        }
        return cls(
            audit_id=audit_id,
            candidate_id=candidate_id,
            status=status,
            evidence_package_reference=evidence_package_reference,
            evidence_hash=evidence_hash,
            reason_codes=normalized_reasons,
            issued_at=issued_at,
            auditor_id=auditor_id,
            contract_version=contract_version,
            signature=signature,
            decision_hash=canonical_sha256(payload),
        )


@dataclass(frozen=True, slots=True)
class StrategyPackage:
    strategy_id: str
    version: str
    symbol: str
    exchange: str
    market_type: MarketType
    directions: frozenset[Direction]
    architecture: StrategyArchitecture
    parameters: Mapping[str, Any]
    risk_policy: Mapping[str, Any]
    constraints: Mapping[str, Any]
    timeframes: tuple[str, ...]
    execution_assumptions: Mapping[str, Any]
    dataset_hashes: tuple[str, ...]
    research_periods: Mapping[str, TimeRange]
    commit_sha: str
    random_seeds: tuple[int, ...]
    key_results: Mapping[str, Any]
    robustness_report: RobustnessReport
    holdout_report: HoldoutReport
    known_weaknesses: tuple[str, ...]
    disabled_regimes: tuple[str, ...]
    admission_status: AdmissionStatus
    evidence_links: tuple[str, ...]
    paper_configuration: Mapping[str, Any]
    package_hash: str
    live_trading_authorized: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        _id(self.strategy_id, "strategy_id")
        if not _VERSION_RE.fullmatch(self.version):
            raise ContractError("strategy package version is invalid")
        _id(self.symbol, "symbol")
        _id(self.exchange, "exchange")
        if not self.directions:
            raise ContractError("strategy package requires directions")
        for digest in self.dataset_hashes:
            _sha(digest, "dataset hash")
        _git_sha(self.commit_sha, "commit_sha")
        _sha(self.package_hash, "package_hash")
        if self.live_trading_authorized:
            raise ContractError("Strategy Package must never authorize live trading")
        if self.admission_status is AdmissionStatus.APPROVED_FOR_PAPER:
            if not self.robustness_report.passed or not self.holdout_report.passed:
                raise ContractError("paper admission requires robustness and holdout passes")
        for name in (
            "parameters",
            "risk_policy",
            "constraints",
            "execution_assumptions",
            "key_results",
            "paper_configuration",
        ):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "research_periods", freeze(self.research_periods))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyPackage schema_version")


@dataclass(frozen=True, slots=True)
class StrategyFactoryCheckpoint:
    run_id: str
    request_fingerprint: str
    status: FactoryStatus
    stage: FactoryStage
    stage_sequence: int
    dataset_fingerprint: str
    commit_sha: str
    manifest_id: str
    completed_artifacts: Mapping[str, str]
    submitted_batches: Mapping[str, str]
    processed_event_ids: frozenset[str]
    budget_state: Mapping[str, Any]
    decision_chain_head: str | None
    updated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("run_id", "manifest_id"):
            _id(getattr(self, name), name)
        _sha(self.request_fingerprint, "request_fingerprint")
        _sha(self.dataset_fingerprint, "dataset_fingerprint")
        _git_sha(self.commit_sha, "commit_sha")
        _positive_int(self.stage_sequence, "stage_sequence", allow_zero=True)
        if self.decision_chain_head is not None:
            _sha(self.decision_chain_head, "decision_chain_head")
        for name in ("completed_artifacts", "submitted_batches", "budget_state"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyFactoryCheckpoint schema_version")


@dataclass(frozen=True, slots=True)
class StrategyFactoryManifest:
    manifest_id: str
    run_id: str
    request_fingerprint: str
    dataset_fingerprint: str
    commit_sha: str
    core_version: str
    research_runner_version: str
    results_registry_version: str
    strategy_factory_version: str
    module_versions: Mapping[str, str]
    random_seed: int
    created_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("manifest_id", "run_id"):
            _id(getattr(self, name), name)
        for name in ("request_fingerprint", "dataset_fingerprint"):
            _sha(getattr(self, name), name)
        _git_sha(self.commit_sha, "commit_sha")
        _positive_int(self.random_seed, "random_seed", allow_zero=True)
        object.__setattr__(self, "module_versions", freeze(self.module_versions))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.schema_version != 1:
            raise ContractError("unsupported StrategyFactoryManifest schema_version")
