from __future__ import annotations

import math

import pytest

from modular_results_registry.errors import ValidationError
from modular_results_registry.findings import (
    StabilityInput,
    StabilityThresholds,
    StatisticalStrengthInput,
    evaluate_stability,
    evaluate_statistical_strength,
)
from modular_results_registry.models import FindingCode, Severity


def _codes(findings):
    return {item.code for item in findings}


def _healthy_stability(**changes) -> StabilityInput:
    values = dict(
        candidate_id="candidate-001",
        period_contributions={"a": 6, "b": 5},
        instrument_contributions={"BTC": 6, "ETH": 5},
        side_contributions={"LONG": 6, "SHORT": 5},
        module_contributions={"m1": 6, "m2": 5},
        regime_contributions={"trend": 6, "range": 5},
        trade_pnls=(3, 3, 3, 3),
        oos_degradation=0.1,
        cost_degradation=0.1,
        walk_forward_dispersion=0.1,
        seed_dispersion=0.1,
        parameter_dispersion=0.1,
        robustness_completed=True,
    )
    values.update(changes)
    return StabilityInput(**values)


def _healthy_statistical(**changes) -> StatisticalStrengthInput:
    values = dict(
        candidate_id="candidate-001",
        trade_count=100,
        oos_trade_count=30,
        period_days=365,
        independent_regimes=3,
        confidence_interval_width=1.0,
        bootstrap_present=True,
        monte_carlo_present=True,
        untouched_test_present=True,
        contaminated=False,
    )
    values.update(changes)
    return StatisticalStrengthInput(**values)


def test_healthy_inputs_produce_no_findings() -> None:
    assert evaluate_stability(_healthy_stability()) == ()
    assert evaluate_statistical_strength(_healthy_statistical()) == ()


def test_stability_emits_every_finding_family_deterministically() -> None:
    findings = evaluate_stability(
        StabilityInput(
            candidate_id="candidate-001",
            period_contributions={"dominant": 9, "other": 1},
            instrument_contributions={"dominant": 8, "other": 2},
            side_contributions={"dominant": 7, "other": 3},
            module_contributions={"dominant": 6, "other": 4},
            regime_contributions={"dominant": 9.5, "other": 0.5},
            trade_pnls=(8, 1, 1),
            oos_degradation=0.60,
            cost_degradation=0.61,
            walk_forward_dispersion=0.70,
            seed_dispersion=0.40,
            parameter_dispersion=0.50,
            robustness_completed=False,
        )
    )
    assert _codes(findings) == {
        FindingCode.PERIOD_CONCENTRATION,
        FindingCode.INSTRUMENT_CONCENTRATION,
        FindingCode.SIDE_CONCENTRATION,
        FindingCode.MODULE_CONCENTRATION,
        FindingCode.REGIME_CONCENTRATION,
        FindingCode.TOP_TRADE_DEPENDENCE,
        FindingCode.OOS_DEGRADATION,
        FindingCode.COST_SENSITIVITY,
        FindingCode.WALK_FORWARD_INSTABILITY,
        FindingCode.SEED_INSTABILITY,
        FindingCode.PARAMETER_INSTABILITY,
        FindingCode.INSUFFICIENT_ROBUSTNESS,
    }
    assert [item.code.value for item in findings] == sorted(item.code.value for item in findings)
    by_code = {item.code: item for item in findings}
    assert by_code[FindingCode.PERIOD_CONCENTRATION].blocking is True
    assert by_code[FindingCode.INSTRUMENT_CONCENTRATION].severity is Severity.HIGH
    assert by_code[FindingCode.SIDE_CONCENTRATION].severity is Severity.MEDIUM
    assert by_code[FindingCode.MODULE_CONCENTRATION].blocking is False
    assert by_code[FindingCode.TOP_TRADE_DEPENDENCE].blocking is True
    assert by_code[FindingCode.OOS_DEGRADATION].blocking is True


def test_concentration_ignores_nonpositive_values_and_uses_label_tie_break() -> None:
    assert evaluate_stability(_healthy_stability(period_contributions={"a": -1, "b": 0})) == ()
    findings = evaluate_stability(
        _healthy_stability(
            period_contributions={"z": 1, "a": 1},
            robustness_completed=True,
        ),
        StabilityThresholds(concentration_share=0.5),
    )
    item = next(value for value in findings if value.code is FindingCode.PERIOD_CONCENTRATION)
    assert item.affected_scope["dominant_label"] == "z"
    assert item.actual_values["share"] == 0.5


@pytest.mark.parametrize(
    ("pnl", "severity", "blocking"),
    [
        ((4, 3, 3), Severity.MEDIUM, False),
        ((6, 2, 2), Severity.HIGH, False),
        ((8, 1, 1), Severity.HIGH, True),
    ],
)
def test_top_trade_thresholds(pnl, severity, blocking) -> None:
    findings = evaluate_stability(_healthy_stability(trade_pnls=pnl))
    item = next(value for value in findings if value.code is FindingCode.TOP_TRADE_DEPENDENCE)
    assert item.severity is severity
    assert item.blocking is blocking


def test_statistical_strength_reports_sample_evidence_and_contamination() -> None:
    findings = evaluate_statistical_strength(
        _healthy_statistical(
            trade_count=99,
            oos_trade_count=29,
            period_days=364,
            independent_regimes=2,
            confidence_interval_width=None,
            bootstrap_present=False,
            monte_carlo_present=False,
            untouched_test_present=False,
            contaminated=True,
        )
    )
    assert _codes(findings) == {
        FindingCode.INSUFFICIENT_SAMPLE,
        FindingCode.MISSING_EVIDENCE,
        FindingCode.TEST_CONTAMINATION,
    }
    by_code = {item.code: item for item in findings}
    assert set(by_code[FindingCode.INSUFFICIENT_SAMPLE].actual_values) == {
        "trade_count",
        "oos_trade_count",
        "period_days",
        "independent_regimes",
        "confidence_interval_width",
    }
    assert by_code[FindingCode.MISSING_EVIDENCE].actual_values["missing"] == (
        "bootstrap",
        "monte_carlo",
        "untouched_test",
    )
    assert by_code[FindingCode.TEST_CONTAMINATION].severity is Severity.CRITICAL


@pytest.mark.parametrize(
    "kwargs",
    [
        {"concentration_share": -0.1},
        {"concentration_share": 1.1},
        {"top_trade_share": 1.1},
        {"seed_dispersion": math.inf},
        {"parameter_dispersion": True},
    ],
)
def test_threshold_validation(kwargs) -> None:
    with pytest.raises(ValidationError):
        StabilityThresholds(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_id": " "},
        {"period_contributions": []},
        {"period_contributions": {" ": 1}},
        {"period_contributions": {"a": math.nan}},
        {"trade_pnls": [1.0]},
        {"trade_pnls": (math.inf,)},
        {"walk_forward_dispersion": -0.1},
        {"robustness_completed": 1},
    ],
)
def test_stability_input_validation(kwargs) -> None:
    with pytest.raises(ValidationError):
        _healthy_stability(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_id": ""},
        {"trade_count": True},
        {"oos_trade_count": -1},
        {"minimum_trade_count": 0},
        {"confidence_interval_width": -0.1},
        {"maximum_confidence_interval_width": math.inf},
        {"bootstrap_present": 1},
        {"contaminated": "false"},
    ],
)
def test_statistical_input_validation(kwargs) -> None:
    with pytest.raises(ValidationError):
        _healthy_statistical(**kwargs)
