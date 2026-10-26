"""Candidate evidence gates, multiple-testing correction, and holdout admission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .canonical import canonical_sha256, freeze
from .contracts import CandidateStrategy, RobustnessReport
from .errors import CompatibilityError, ContractError

_HOLDOUT_TOKENS = ("holdout", "final_untouched", "untouched_test")


@dataclass(frozen=True, slots=True)
class CandidateGatePolicy:
    """Explicit finalist requirements; no opaque aggregate score is used."""

    minimum_trade_count: int
    maximum_drawdown: Decimal
    minimum_profit_factor: Decimal = Decimal("1")
    minimum_walk_forward_efficiency: Decimal = Decimal("0.5")
    minimum_parameter_stability: Decimal = Decimal("0.6")
    minimum_regime_coverage: Decimal = Decimal("0.5")
    minimum_time_coverage: Decimal = Decimal("0.8")
    minimum_cost_stress_profit_factor: Decimal = Decimal("1")
    minimum_year_stability: Decimal = Decimal("0.5")
    maximum_complexity: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_trade_count, bool)
            or not isinstance(self.minimum_trade_count, int)
            or self.minimum_trade_count < 1
        ):
            raise ContractError("minimum_trade_count must be a positive integer")
        for name in (
            "maximum_drawdown",
            "minimum_profit_factor",
            "minimum_walk_forward_efficiency",
            "minimum_parameter_stability",
            "minimum_regime_coverage",
            "minimum_time_coverage",
            "minimum_cost_stress_profit_factor",
            "minimum_year_stability",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ContractError(f"{name} must be a finite non-negative Decimal")
        for name in (
            "minimum_walk_forward_efficiency",
            "minimum_parameter_stability",
            "minimum_regime_coverage",
            "minimum_time_coverage",
            "minimum_year_stability",
        ):
            if getattr(self, name) > 1:
                raise ContractError(f"{name} must not exceed 1")
        if self.maximum_drawdown > 1:
            raise ContractError("maximum_drawdown must not exceed 1")
        if self.maximum_complexity is not None and (
            not isinstance(self.maximum_complexity, Decimal)
            or not self.maximum_complexity.is_finite()
            or self.maximum_complexity < 0
        ):
            raise ContractError("maximum_complexity must be a finite non-negative Decimal")


@dataclass(frozen=True, slots=True)
class CandidateGateResult:
    candidate_id: str
    eligible: bool
    checks: dict[str, bool]
    evidence: dict[str, Decimal | int | str]
    reason_codes: tuple[str, ...]
    policy_fingerprint: str

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ContractError("candidate_id cannot be empty")
        if not isinstance(self.eligible, bool):
            raise ContractError("eligible must be bool")
        object.__setattr__(self, "checks", freeze(self.checks))
        object.__setattr__(self, "evidence", freeze(self.evidence))
        if self.eligible != all(self.checks.values()):
            raise ContractError("candidate gate eligibility disagrees with checks")
        if self.eligible and self.reason_codes:
            raise ContractError("eligible candidate cannot contain rejection reasons")
        if not self.eligible and not self.reason_codes:
            raise ContractError("ineligible candidate requires rejection reasons")
        _sha(self.policy_fingerprint, "policy_fingerprint")


class HoldoutIsolationGuard:
    """Reject selection evidence that already exposes final untouched results."""

    @staticmethod
    def assert_unexposed(candidate: CandidateStrategy) -> None:
        evaluation = candidate.evaluation
        if evaluation is None:
            raise CompatibilityError("candidate has no evaluation")
        contaminated = sorted(
            key
            for key in evaluation.metrics
            if any(token in key.lower() for token in _HOLDOUT_TOKENS)
        )
        contaminated.extend(
            reference
            for reference in (*candidate.lineage_references, *evaluation.evidence_references)
            if any(token in reference.lower() for token in _HOLDOUT_TOKENS)
        )
        if contaminated:
            raise CompatibilityError(
                "candidate selection evidence contains holdout references: "
                + ", ".join(sorted(set(contaminated)))
            )


class CandidateGate:
    """Evaluate one candidate against explicit stability and evidence requirements."""

    def __init__(self, policy: CandidateGatePolicy) -> None:
        self._policy = policy

    def evaluate(self, candidate: CandidateStrategy) -> CandidateGateResult:
        HoldoutIsolationGuard.assert_unexposed(candidate)
        evaluation = candidate.evaluation
        assert evaluation is not None
        checks: dict[str, bool] = {}
        evidence: dict[str, Decimal | int | str] = {}
        reasons: list[str] = []
        requirements: tuple[tuple[str, Decimal, str, str], ...] = (
            ("profit_factor", self._policy.minimum_profit_factor, "minimum", "LOW_PROFIT_FACTOR"),
            (
                "walk_forward_efficiency",
                self._policy.minimum_walk_forward_efficiency,
                "minimum",
                "UNSTABLE_WALK_FORWARD",
            ),
            (
                "parameter_stability",
                self._policy.minimum_parameter_stability,
                "minimum",
                "PARAMETER_INSTABILITY",
            ),
            (
                "regime_coverage",
                self._policy.minimum_regime_coverage,
                "minimum",
                "INSUFFICIENT_REGIME_COVERAGE",
            ),
            (
                "time_coverage",
                self._policy.minimum_time_coverage,
                "minimum",
                "INSUFFICIENT_TIME_COVERAGE",
            ),
            (
                "cost_stress_profit_factor",
                self._policy.minimum_cost_stress_profit_factor,
                "minimum",
                "COST_SENSITIVITY",
            ),
            (
                "year_stability",
                self._policy.minimum_year_stability,
                "minimum",
                "UNSTABLE_YEARLY_RESULTS",
            ),
            (
                "max_drawdown",
                self._policy.maximum_drawdown,
                "maximum",
                "DRAWDOWN_LIMIT_EXCEEDED",
            ),
        )
        trade_count = _required_metric(evaluation.metrics, "trade_count")
        integral_trade_count = _nonnegative_integral(trade_count, "trade_count")
        evidence["trade_count"] = integral_trade_count
        checks["minimum_trade_count"] = integral_trade_count >= self._policy.minimum_trade_count
        if not checks["minimum_trade_count"]:
            reasons.append("LOW_TRADE_COUNT")
        for metric, threshold, direction, reason in requirements:
            value = _required_metric(evaluation.metrics, metric)
            evidence[metric] = value
            passed = value >= threshold if direction == "minimum" else abs(value) <= threshold
            checks[metric] = passed
            if not passed:
                reasons.append(reason)
        checks["declared_constraints"] = all(evaluation.constraints.values())
        if not checks["declared_constraints"]:
            reasons.append("DECLARED_CONSTRAINT_FAILURE")
        checks["instability_flags"] = not evaluation.instability_flags
        if not checks["instability_flags"]:
            evidence["instability_flags"] = ",".join(evaluation.instability_flags)
            reasons.append("INSTABILITY_FLAGS_PRESENT")
        if self._policy.maximum_complexity is not None:
            evidence["complexity"] = evaluation.complexity
            checks["maximum_complexity"] = evaluation.complexity <= self._policy.maximum_complexity
            if not checks["maximum_complexity"]:
                reasons.append("COMPLEXITY_LIMIT_EXCEEDED")
        return CandidateGateResult(
            candidate_id=candidate.candidate_id,
            eligible=all(checks.values()),
            checks=checks,
            evidence=evidence,
            reason_codes=tuple(sorted(set(reasons))),
            policy_fingerprint=canonical_sha256(self._policy),
        )


@dataclass(frozen=True, slots=True)
class MultipleTestingResult:
    adjusted_alpha: Decimal
    adjusted_p_values: dict[str, Decimal]
    accepted_candidate_ids: tuple[str, ...]
    rejected_candidate_ids: tuple[str, ...]
    method: str = "holm_bonferroni"

    def __post_init__(self) -> None:
        if self.method != "holm_bonferroni":
            raise ContractError("unsupported multiple-testing method")
        if not Decimal("0") < self.adjusted_alpha < Decimal("1"):
            raise ContractError("adjusted_alpha must be in (0, 1)")
        object.__setattr__(self, "adjusted_p_values", freeze(self.adjusted_p_values))
        if set(self.accepted_candidate_ids) & set(self.rejected_candidate_ids):
            raise ContractError("multiple-testing result contains overlapping decisions")


class MultipleTestingGuard:
    """Holm-Bonferroni correction over every viewed candidate hypothesis."""

    def __init__(self, *, family_alpha: Decimal = Decimal("0.05")) -> None:
        if not isinstance(family_alpha, Decimal) or not family_alpha.is_finite():
            raise ContractError("family_alpha must be a finite Decimal")
        if not Decimal("0") < family_alpha < Decimal("1"):
            raise ContractError("family_alpha must be in (0, 1)")
        self._alpha = family_alpha

    def evaluate(self, p_values: dict[str, Decimal]) -> MultipleTestingResult:
        if not p_values:
            raise CompatibilityError("multiple-testing guard requires p-values")
        normalized: list[tuple[str, Decimal]] = []
        for candidate_id, value in p_values.items():
            if not candidate_id or not isinstance(candidate_id, str):
                raise CompatibilityError("candidate p-value ID is invalid")
            if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 1:
                raise CompatibilityError("candidate p-values must be finite Decimals in [0, 1]")
            normalized.append((candidate_id, value))
        normalized.sort(key=lambda item: (item[1], item[0]))
        total = len(normalized)
        adjusted: dict[str, Decimal] = {}
        running = Decimal("0")
        reject_remaining = False
        accepted: list[str] = []
        rejected: list[str] = []
        for index, (candidate_id, value) in enumerate(normalized):
            multiplier = Decimal(total - index)
            running = max(running, min(Decimal("1"), value * multiplier))
            adjusted[candidate_id] = running
            threshold = self._alpha / multiplier
            if not reject_remaining and value <= threshold:
                accepted.append(candidate_id)
            else:
                reject_remaining = True
                rejected.append(candidate_id)
        return MultipleTestingResult(
            adjusted_alpha=self._alpha,
            adjusted_p_values=dict(sorted(adjusted.items())),
            accepted_candidate_ids=tuple(accepted),
            rejected_candidate_ids=tuple(rejected),
        )


class FinalTestAuthorizer(Protocol):
    def authorize_final_test(
        self,
        experiment_id: str,
        *,
        candidate_configuration_hashes: tuple[str, ...],
        issued_at: datetime | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class HoldoutAdmissionDecision:
    experiment_id: str
    candidate_ids: tuple[str, ...]
    configuration_hashes: tuple[str, ...]
    native_configuration_hashes: tuple[str, ...]
    gate_evidence_hash: str
    native_authorization: Any

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ContractError("experiment_id cannot be empty")
        if (
            not self.candidate_ids
            or len(self.candidate_ids) != len(self.configuration_hashes)
            or len(self.candidate_ids) != len(self.native_configuration_hashes)
        ):
            raise ContractError("holdout admission candidate identities are invalid")
        for value in (*self.configuration_hashes, *self.native_configuration_hashes):
            _sha(value, "configuration_hash")
        _sha(self.gate_evidence_hash, "gate_evidence_hash")


class HoldoutAdmissionController:
    """Gate finalists, then delegate one-time access to Research Runner FinalTestGrant."""

    def __init__(self, authorizer: FinalTestAuthorizer, *, maximum_finalists: int) -> None:
        if not callable(getattr(authorizer, "authorize_final_test", None)):
            raise TypeError("authorizer must implement authorize_final_test()")
        if (
            isinstance(maximum_finalists, bool)
            or not isinstance(maximum_finalists, int)
            or maximum_finalists < 1
        ):
            raise ContractError("maximum_finalists must be a positive integer")
        self._authorizer = authorizer
        self._maximum_finalists = maximum_finalists

    def authorize(
        self,
        *,
        experiment_id: str,
        candidates: tuple[CandidateStrategy, ...],
        gates: tuple[CandidateGateResult, ...],
        robustness_reports: tuple[RobustnessReport, ...],
        producer_configuration_hashes: Mapping[str, str] | None = None,
        issued_at: datetime | None = None,
    ) -> HoldoutAdmissionDecision:
        if not candidates or len(candidates) > self._maximum_finalists:
            raise CompatibilityError("holdout finalist count is outside the configured budget")
        if len({item.candidate_id for item in candidates}) != len(candidates):
            raise CompatibilityError("holdout finalists contain duplicate candidate IDs")
        gate_by_id = {item.candidate_id: item for item in gates}
        robustness_by_id = {item.candidate_id: item for item in robustness_reports}
        evidence: dict[str, Any] = {}
        for candidate in candidates:
            HoldoutIsolationGuard.assert_unexposed(candidate)
            gate = gate_by_id.get(candidate.candidate_id)
            robustness = robustness_by_id.get(candidate.candidate_id)
            if gate is None or not gate.eligible:
                raise CompatibilityError("every holdout finalist must pass the candidate gate")
            if robustness is None or not robustness.passed:
                raise CompatibilityError("every holdout finalist must pass robustness validation")
            evidence[candidate.candidate_id] = {
                "configuration_hash": candidate.configuration_hash,
                "gate": gate,
                "robustness_report_id": robustness.report_id,
            }
        ordered = tuple(sorted(candidates, key=lambda item: item.candidate_id))
        configuration_hashes = tuple(item.configuration_hash for item in ordered)
        native_by_id = (
            {item.candidate_id: item.configuration_hash for item in ordered}
            if producer_configuration_hashes is None
            else dict(producer_configuration_hashes)
        )
        if set(native_by_id) != {item.candidate_id for item in ordered}:
            raise CompatibilityError(
                "producer configuration hashes must cover every holdout finalist"
            )
        native_configuration_hashes = tuple(
            _sha(native_by_id[item.candidate_id], "producer_configuration_hash") for item in ordered
        )
        authorization = (
            self._authorizer.authorize_final_test(
                experiment_id,
                candidate_configuration_hashes=native_configuration_hashes,
            )
            if issued_at is None
            else self._authorizer.authorize_final_test(
                experiment_id,
                candidate_configuration_hashes=native_configuration_hashes,
                issued_at=issued_at,
            )
        )
        return HoldoutAdmissionDecision(
            experiment_id=experiment_id,
            candidate_ids=tuple(item.candidate_id for item in ordered),
            configuration_hashes=configuration_hashes,
            native_configuration_hashes=native_configuration_hashes,
            gate_evidence_hash=canonical_sha256(evidence),
            native_authorization=authorization,
        )


def _required_metric(metrics: Any, metric: str) -> Decimal:
    if metric in metrics:
        return _decimal(metrics[metric], metric)
    suffix = f".{metric}"
    matches = [(key, value) for key, value in metrics.items() if key.endswith(suffix)]
    if len(matches) != 1:
        if not matches:
            raise CompatibilityError(f"candidate is missing required metric: {metric}")
        raise CompatibilityError(f"candidate metric is ambiguous across slices: {metric}")
    return _decimal(matches[0][1], metric)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise CompatibilityError(f"candidate metric {name} must be numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CompatibilityError(f"candidate metric {name} must be numeric") from exc
    if not converted.is_finite():
        raise CompatibilityError(f"candidate metric {name} must be finite")
    return converted


def _nonnegative_integral(value: Decimal, name: str) -> int:
    integral = value.to_integral_value()
    if value != integral or integral < 0:
        raise CompatibilityError(f"candidate metric {name} must be a non-negative integer")
    return int(integral)


def _sha(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError(f"{name} must be a lowercase SHA-256 digest")
    return value
