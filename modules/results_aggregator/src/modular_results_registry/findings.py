from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Mapping

from .errors import ValidationError
from .models import Finding, FindingCode, Severity


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _finite(value: object, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{name} must be finite")
    if nonnegative and result < 0:
        raise ValidationError(f"{name} cannot be negative")
    return result


def _count(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValidationError(f"{name} must be {qualifier}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _contribution_mapping(value: Mapping[str, float], name: str) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    normalized: dict[str, float] = {}
    for key, item in value.items():
        label = _nonblank(key, f"{name} key")
        normalized[label] = _finite(item, f"{name}[{label}]")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class StabilityThresholds:
    concentration_share: float = 0.60
    top_trade_share: float = 0.35
    oos_degradation: float = 0.30
    cost_degradation: float = 0.30
    walk_forward_dispersion: float = 0.35
    seed_dispersion: float = 0.20
    parameter_dispersion: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "concentration_share",
            "top_trade_share",
            "oos_degradation",
            "cost_degradation",
            "walk_forward_dispersion",
            "seed_dispersion",
            "parameter_dispersion",
        ):
            value = _finite(getattr(self, name), name, nonnegative=True)
            if name in {"concentration_share", "top_trade_share"} and value > 1:
                raise ValidationError(f"{name} cannot exceed 1")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class StabilityInput:
    candidate_id: str
    period_contributions: Mapping[str, float] = field(default_factory=dict)
    instrument_contributions: Mapping[str, float] = field(default_factory=dict)
    side_contributions: Mapping[str, float] = field(default_factory=dict)
    module_contributions: Mapping[str, float] = field(default_factory=dict)
    regime_contributions: Mapping[str, float] = field(default_factory=dict)
    trade_pnls: tuple[float, ...] = ()
    oos_degradation: float | None = None
    cost_degradation: float | None = None
    walk_forward_dispersion: float | None = None
    seed_dispersion: float | None = None
    parameter_dispersion: float | None = None
    robustness_completed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _nonblank(self.candidate_id, "candidate_id"))
        for name in (
            "period_contributions",
            "instrument_contributions",
            "side_contributions",
            "module_contributions",
            "regime_contributions",
        ):
            object.__setattr__(self, name, _contribution_mapping(getattr(self, name), name))
        if not isinstance(self.trade_pnls, tuple):
            raise ValidationError("trade_pnls must be a tuple")
        object.__setattr__(
            self,
            "trade_pnls",
            tuple(_finite(item, "trade_pnl") for item in self.trade_pnls),
        )
        for name in ("oos_degradation", "cost_degradation"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, name))
        for name in (
            "walk_forward_dispersion",
            "seed_dispersion",
            "parameter_dispersion",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite(value, name, nonnegative=True))
        object.__setattr__(
            self,
            "robustness_completed",
            _boolean(self.robustness_completed, "robustness_completed"),
        )


@dataclass(frozen=True, slots=True)
class StatisticalStrengthInput:
    candidate_id: str
    trade_count: int
    oos_trade_count: int
    period_days: int
    independent_regimes: int
    confidence_interval_width: float | None
    bootstrap_present: bool
    monte_carlo_present: bool
    untouched_test_present: bool
    contaminated: bool
    minimum_trade_count: int = 100
    minimum_oos_trade_count: int = 30
    minimum_period_days: int = 365
    minimum_regimes: int = 3
    maximum_confidence_interval_width: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _nonblank(self.candidate_id, "candidate_id"))
        for name in ("trade_count", "oos_trade_count", "period_days", "independent_regimes"):
            object.__setattr__(self, name, _count(getattr(self, name), name))
        for name in (
            "minimum_trade_count",
            "minimum_oos_trade_count",
            "minimum_period_days",
            "minimum_regimes",
        ):
            object.__setattr__(self, name, _count(getattr(self, name), name, positive=True))
        if self.confidence_interval_width is not None:
            object.__setattr__(
                self,
                "confidence_interval_width",
                _finite(
                    self.confidence_interval_width,
                    "confidence_interval_width",
                    nonnegative=True,
                ),
            )
        object.__setattr__(
            self,
            "maximum_confidence_interval_width",
            _finite(
                self.maximum_confidence_interval_width,
                "maximum_confidence_interval_width",
                nonnegative=True,
            ),
        )
        for name in (
            "bootstrap_present",
            "monte_carlo_present",
            "untouched_test_present",
            "contaminated",
        ):
            object.__setattr__(self, name, _boolean(getattr(self, name), name))


def _concentration_finding(
    *,
    code: FindingCode,
    candidate_id: str,
    values: Mapping[str, float],
    threshold: float,
) -> Finding | None:
    positive_total = sum(max(0.0, value) for value in values.values())
    if positive_total <= 0 or not values:
        return None
    label, contribution = max(values.items(), key=lambda item: (max(0.0, item[1]), item[0]))
    share = max(0.0, contribution) / positive_total
    if share < threshold:
        return None
    return Finding.create(
        code=code,
        severity=Severity.HIGH if share >= 0.80 else Severity.MEDIUM,
        blocking=share >= 0.90,
        affected_scope={"candidate_id": candidate_id, "dominant_label": label},
        thresholds={"maximum_share": threshold},
        actual_values={"share": share, "contributions": dict(values)},
        recommendation="Review the dominant segment and require broader independent support.",
    )


def evaluate_stability(
    data: StabilityInput,
    thresholds: StabilityThresholds = StabilityThresholds(),
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for code, values in (
        (FindingCode.PERIOD_CONCENTRATION, data.period_contributions),
        (FindingCode.INSTRUMENT_CONCENTRATION, data.instrument_contributions),
        (FindingCode.SIDE_CONCENTRATION, data.side_contributions),
        (FindingCode.MODULE_CONCENTRATION, data.module_contributions),
        (FindingCode.REGIME_CONCENTRATION, data.regime_contributions),
    ):
        finding = _concentration_finding(
            code=code,
            candidate_id=data.candidate_id,
            values=values,
            threshold=thresholds.concentration_share,
        )
        if finding is not None:
            findings.append(finding)

    positive_total = sum(max(0.0, value) for value in data.trade_pnls)
    if positive_total > 0:
        top_share = (
            max((max(0.0, value) for value in data.trade_pnls), default=0.0) / positive_total
        )
        if top_share >= thresholds.top_trade_share:
            findings.append(
                Finding.create(
                    code=FindingCode.TOP_TRADE_DEPENDENCE,
                    severity=Severity.HIGH if top_share >= 0.60 else Severity.MEDIUM,
                    blocking=top_share >= 0.80,
                    affected_scope={"candidate_id": data.candidate_id},
                    thresholds={"maximum_top_trade_share": thresholds.top_trade_share},
                    actual_values={
                        "top_trade_share": top_share,
                        "trade_count": len(data.trade_pnls),
                    },
                    recommendation="Re-evaluate performance after removing the largest winning trades.",
                )
            )

    checks = (
        (FindingCode.OOS_DEGRADATION, data.oos_degradation, thresholds.oos_degradation),
        (FindingCode.COST_SENSITIVITY, data.cost_degradation, thresholds.cost_degradation),
        (
            FindingCode.WALK_FORWARD_INSTABILITY,
            data.walk_forward_dispersion,
            thresholds.walk_forward_dispersion,
        ),
        (FindingCode.SEED_INSTABILITY, data.seed_dispersion, thresholds.seed_dispersion),
        (
            FindingCode.PARAMETER_INSTABILITY,
            data.parameter_dispersion,
            thresholds.parameter_dispersion,
        ),
    )
    for code, value, threshold in checks:
        if value is not None and value >= threshold:
            findings.append(
                Finding.create(
                    code=code,
                    severity=Severity.HIGH,
                    blocking=value >= threshold * 2,
                    affected_scope={"candidate_id": data.candidate_id},
                    thresholds={"maximum": threshold},
                    actual_values={"value": value},
                    recommendation="Investigate the instability before candidate promotion.",
                )
            )
    if not data.robustness_completed:
        findings.append(
            Finding.create(
                code=FindingCode.INSUFFICIENT_ROBUSTNESS,
                severity=Severity.HIGH,
                blocking=True,
                affected_scope={"candidate_id": data.candidate_id},
                thresholds={"required": True},
                actual_values={"completed": False},
                recommendation="Complete the required robustness suite.",
            )
        )
    return tuple(sorted(findings, key=lambda item: (item.code.value, item.finding_id)))


def evaluate_statistical_strength(data: StatisticalStrengthInput) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    weaknesses: dict[str, object] = {}
    if data.trade_count < data.minimum_trade_count:
        weaknesses["trade_count"] = data.trade_count
    if data.oos_trade_count < data.minimum_oos_trade_count:
        weaknesses["oos_trade_count"] = data.oos_trade_count
    if data.period_days < data.minimum_period_days:
        weaknesses["period_days"] = data.period_days
    if data.independent_regimes < data.minimum_regimes:
        weaknesses["independent_regimes"] = data.independent_regimes
    if (
        data.confidence_interval_width is None
        or data.confidence_interval_width > data.maximum_confidence_interval_width
    ):
        weaknesses["confidence_interval_width"] = data.confidence_interval_width
    if weaknesses:
        findings.append(
            Finding.create(
                code=FindingCode.INSUFFICIENT_SAMPLE,
                severity=Severity.HIGH,
                blocking=True,
                affected_scope={"candidate_id": data.candidate_id},
                thresholds={
                    "minimum_trade_count": data.minimum_trade_count,
                    "minimum_oos_trade_count": data.minimum_oos_trade_count,
                    "minimum_period_days": data.minimum_period_days,
                    "minimum_regimes": data.minimum_regimes,
                    "maximum_confidence_interval_width": data.maximum_confidence_interval_width,
                },
                actual_values=weaknesses,
                recommendation="Increase independent OOS evidence before audit promotion.",
            )
        )
    missing = [
        name
        for name, present in (
            ("bootstrap", data.bootstrap_present),
            ("monte_carlo", data.monte_carlo_present),
            ("untouched_test", data.untouched_test_present),
        )
        if not present
    ]
    if missing:
        findings.append(
            Finding.create(
                code=FindingCode.MISSING_EVIDENCE,
                severity=Severity.HIGH,
                blocking=True,
                affected_scope={"candidate_id": data.candidate_id},
                thresholds={"required_evidence": ("bootstrap", "monte_carlo", "untouched_test")},
                actual_values={"missing": tuple(missing)},
                recommendation="Generate the missing evidence with the official validation subsystem.",
            )
        )
    if data.contaminated:
        findings.append(
            Finding.create(
                code=FindingCode.TEST_CONTAMINATION,
                severity=Severity.CRITICAL,
                blocking=True,
                affected_scope={"candidate_id": data.candidate_id},
                thresholds={"contaminated": False},
                actual_values={"contaminated": True},
                recommendation="Exclude contaminated results from release comparisons.",
            )
        )
    return tuple(sorted(findings, key=lambda item: (item.code.value, item.finding_id)))
