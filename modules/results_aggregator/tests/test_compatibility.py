from __future__ import annotations


import pytest

from modular_results_registry.compatibility import (
    ComparisonContext,
    check_compatibility,
    compare_metrics,
)
from modular_results_registry.errors import CompatibilityError
from modular_results_registry.models import ComparisonStatus, MetricSnapshot


def _context(**changes: object) -> ComparisonContext:
    values: dict[str, object] = {
        "market": "BINANCE_USDTM_FUTURES",
        "instrument": "BTCUSDT",
        "timeframe": "5m",
        "period_start": "2020-01-01T00:00:00Z",
        "period_end": "2026-01-01T00:00:00Z",
        "dataset_hashes": ("a" * 64,),
        "split_id": "split-test",
        "execution_model": "conservative-v1",
        "commission_model": "fees-v1",
        "slippage_model": "slippage-v1",
        "funding_model": "funding-v1",
        "risk_setting": "0.5%",
        "metric_definition_version": "metrics-v1",
        "strategy_scope": "full",
        "currency": "USDT",
        "compounding_mode": "compounded",
    }
    values.update(changes)
    return ComparisonContext(**values)  # type: ignore[arg-type]


def _metric(run_id: str, name: str, value: object) -> MetricSnapshot:
    return MetricSnapshot(
        run_id=run_id,
        metric_name=name,
        value=value,  # type: ignore[arg-type]
        unit="ratio",
        precision=6,
        definition_version="metrics-v1",
        producer="core",
        split="test",
        period="all",
        scenario="base",
    )


def test_compatibility_exact_match() -> None:
    result = check_compatibility(_context(), _context())
    assert result.status is ComparisonStatus.COMPARABLE
    assert not result.blocking_mismatches


def test_dataset_and_cost_mismatch_block_comparison() -> None:
    result = check_compatibility(
        _context(),
        _context(dataset_hashes=("b" * 64,), commission_model="fees-v2"),
    )
    assert result.status is ComparisonStatus.NON_COMPARABLE_RESULTS
    assert set(result.blocking_mismatches) == {"commission_model", "dataset_hashes"}


def test_explicit_partial_fields_do_not_silently_become_comparable() -> None:
    result = check_compatibility(
        _context(),
        _context(risk_setting="2.0%"),
        partially_comparable_fields=frozenset({"risk_setting"}),
    )
    assert result.status is ComparisonStatus.PARTIALLY_COMPARABLE
    assert result.partial_mismatches == ("risk_setting",)


def test_compare_metrics_preserves_missing_and_delta_types() -> None:
    compatibility = check_compatibility(_context(), _context())
    comparison = compare_metrics(
        (_metric("run-baseline", "profit_factor", 1.2), _metric("run-baseline", "only_base", 3)),
        (
            _metric("run-candidate", "profit_factor", 1.5),
            _metric("run-candidate", "only_candidate", 4),
        ),
        compatibility,
    )
    by_name = {item.metric_name: item for item in comparison}
    assert by_name["profit_factor"].absolute_delta == pytest.approx(0.3)
    assert by_name["profit_factor"].relative_delta_pct == pytest.approx(25.0)
    assert by_name["only_base"].candidate_value is None
    assert by_name["only_candidate"].baseline_value is None


def test_noncomparable_results_block_metric_deltas() -> None:
    compatibility = check_compatibility(_context(), _context(currency="USD"))
    with pytest.raises(CompatibilityError):
        compare_metrics(
            (_metric("run-baseline", "profit_factor", 1.2),),
            (_metric("run-candidate", "profit_factor", 1.5),),
            compatibility,
        )


def test_non_numeric_metric_is_preserved_without_fake_delta() -> None:
    comparison = compare_metrics(
        (_metric("run-baseline", "status_metric", "PASS"),),
        (_metric("run-candidate", "status_metric", "FAIL"),),
        check_compatibility(_context(), _context()),
    )
    assert comparison[0].absolute_delta is None
    assert comparison[0].relative_delta_pct is None
