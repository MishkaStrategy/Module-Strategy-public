from __future__ import annotations

import json
from dataclasses import replace
from datetime import timezone

import pytest

from conftest import make_package
from modular_results_registry.canonical import canonical_json_bytes
from modular_results_registry.cli_analytics import (
    build_audit_package,
    comparison_context,
    finding_from_mapping,
    profile,
    run_compare,
    run_findings,
    run_pareto,
    run_slices,
)
from modular_results_registry.errors import ValidationError
from modular_results_registry.models import (
    ComparisonStatus,
    FindingCode,
    ObjectiveDirection,
)

UTC = timezone.utc


def _mapping(value):
    return json.loads(canonical_json_bytes(value))


def _context(**changes):
    values = dict(
        market="BINANCE",
        instrument="BTCUSDT",
        timeframe="5m",
        period_start="2020-01-01T00:00:00Z",
        period_end="2026-07-01T00:00:00Z",
        dataset_hashes=["a" * 64],
        split_id="split-001",
        execution_model="conservative",
        commission_model="maker-taker-v1",
        slippage_model="adverse-v1",
        funding_model="base-v1",
        risk_setting="0.5%",
        metric_definition_version="v1",
        strategy_scope="strategy-001",
        currency="USDT",
        compounding_mode="compound",
    )
    values.update(changes)
    return values


def test_profile_and_comparison_context() -> None:
    result = profile(
        {
            "producer_module_id": "core",
            "allowed_producer_schema_versions": ["1"],
            "allowed_artifact_schema_prefixes": ["core."],
            "required_artifact_paths": ["manifest.json"],
            "allowed_package_schema_versions": ["1", "2"],
        }
    )
    assert result.producer_module_id == "core"
    assert result.required_artifact_paths == frozenset({"manifest.json"})
    context = comparison_context(_context(), "context")
    assert context.instrument == "BTCUSDT"
    assert context.dataset_hashes == ("a" * 64,)


def test_run_compare_builds_comparable_metric_rows() -> None:
    package = make_package()
    definition = package.metric_definitions[0]
    baseline = package.metrics[0]
    candidate = replace(baseline, run_id="run-002", value=2.0)
    result = run_compare(
        {
            "baseline_context": _context(),
            "candidate_context": _context(),
            "baseline_metrics": [_mapping(baseline)],
            "candidate_metrics": [_mapping(candidate)],
            "baseline_metric_definitions": [_mapping(definition)],
            "candidate_metric_definitions": [_mapping(definition)],
        }
    )
    assert result.compatibility.status is ComparisonStatus.COMPARABLE
    assert len(result.deltas) == 1
    assert result.deltas[0].metric_name == "profit_factor"
    assert result.deltas[0].absolute_delta == 0.5


def test_run_compare_partial_context_is_preserved() -> None:
    package = make_package()
    result = run_compare(
        {
            "baseline_context": _context(),
            "candidate_context": _context(currency="USD"),
            "partially_comparable_fields": ["currency"],
            "baseline_metrics": [_mapping(package.metrics[0])],
            "candidate_metrics": [_mapping(package.metrics[0])],
            "baseline_metric_definitions": [_mapping(package.metric_definitions[0])],
            "candidate_metric_definitions": [_mapping(package.metric_definitions[0])],
        }
    )
    assert result.compatibility.status is ComparisonStatus.PARTIALLY_COMPARABLE
    assert result.compatibility.partial_mismatches == ("currency",)


def test_run_slices_handles_optional_labels_and_time_parsing() -> None:
    result = run_slices(
        {
            "dimension": "MODULE",
            "trades": [
                {
                    "trade_id": "trade-001",
                    "run_id": "run-001",
                    "instrument": "BTCUSDT",
                    "side": "LONG",
                    "entry_time": "2026-01-01T00:00:00Z",
                    "exit_time": "2026-01-01T01:00:00+00:00",
                    "net_pnl": "10",
                    "fees": "1",
                    "r_multiple": "2",
                    "module": "FVG",
                    "setup": None,
                    "regime": None,
                    "exit_reason": "TP",
                    "source_artifact_hash": "b" * 64,
                },
                {
                    "trade_id": "trade-002",
                    "run_id": "run-001",
                    "instrument": "BTCUSDT",
                    "side": "SHORT",
                    "entry_time": "2026-01-02T00:00:00Z",
                    "exit_time": "2026-01-02T01:00:00Z",
                    "net_pnl": -2,
                },
            ],
        }
    )
    assert result.total_input_trades == 2
    assert result.included_trades == 1
    assert result.missing_label_trades == 1
    assert result.summaries[0].key == "FVG"


def test_run_pareto_builds_frontier_and_exclusions() -> None:
    package = make_package()
    definition = replace(package.metric_definitions[0], direction=ObjectiveDirection.MAXIMIZE)
    result = run_pareto(
        {
            "metric_definitions": [_mapping(definition)],
            "objective_metrics": ["profit_factor"],
            "epsilons": {"profit_factor": "0"},
            "candidates": [
                {
                    "candidate_id": "candidate-a",
                    "metrics": {"profit_factor": 2.0},
                    "constraints": [],
                    "warnings_count": 0,
                    "oos_strength": "1",
                    "robustness_strength": "1",
                    "parameter_sensitivity": "0",
                    "complexity": "1",
                    "trade_count": 100,
                    "configuration_hash": "a" * 64,
                },
                {
                    "candidate_id": "candidate-b",
                    "metrics": {"profit_factor": 1.0},
                    "constraints": [
                        {
                            "name": "min_trades",
                            "passed": False,
                            "blocking": True,
                            "actual_value": 10,
                            "threshold": 100,
                            "reason": "too few",
                        }
                    ],
                    "configuration_hash": "b" * 64,
                },
            ],
            "missing_policy": "EXCLUDE",
        }
    )
    assert result.frontier == ("candidate-a",)
    assert "candidate-b" in result.excluded


def test_run_findings_and_mapping_round_trip() -> None:
    result = run_findings(
        {
            "stability": {
                "candidate_id": "candidate-001",
                "period_contributions": {"2025": 9, "2026": 1},
                "trade_pnls": [8, 1, 1],
                "robustness_completed": False,
            },
            "statistical_strength": {
                "candidate_id": "candidate-001",
                "trade_count": 10,
                "oos_trade_count": 2,
                "period_days": 30,
                "independent_regimes": 1,
                "confidence_interval_width": None,
                "bootstrap_present": False,
                "monte_carlo_present": False,
                "untouched_test_present": False,
                "contaminated": True,
            },
        }
    )
    assert FindingCode.PERIOD_CONCENTRATION in {item.code for item in result["stability_findings"]}
    assert FindingCode.TEST_CONTAMINATION in {item.code for item in result["statistical_findings"]}
    finding = result["stability_findings"][0]
    assert finding_from_mapping(_mapping(finding)) == finding


def test_build_audit_package_from_cli_document() -> None:
    package = make_package()
    finding = run_findings(
        {
            "stability": {
                "candidate_id": "candidate-001",
                "trade_pnls": [],
                "robustness_completed": True,
            },
            "statistical_strength": {
                "candidate_id": "candidate-001",
                "trade_count": 100,
                "oos_trade_count": 30,
                "period_days": 365,
                "independent_regimes": 3,
                "confidence_interval_width": 1,
                "bootstrap_present": True,
                "monte_carlo_present": True,
                "untouched_test_present": True,
                "contaminated": False,
            },
        }
    )["stability_findings"]
    definition = package.metric_definitions[0]
    evidence = package.evidence[0]
    result = build_audit_package(
        {
            "registry_revision": 1,
            "registry_state_hash": "a" * 64,
            "strategy_id": "strategy-001",
            "baseline_ids": [package.configurations[0].configuration_id],
            "candidate_ids": [package.configurations[0].configuration_id],
            "experiment_ids": [package.experiments[0].experiment_id],
            "run_ids": [package.runs[0].run_id],
            "configuration_ids": [package.configurations[0].configuration_id],
            "dataset_ids": [package.datasets[0].dataset_id],
            "dataset_hashes": [package.datasets[0].sha256],
            "commit_shas": [package.experiments[0].commit_sha],
            "module_versions": {"core": "0.4.0"},
            "backtest_engine_versions": [package.runs[0].backtest_engine_version],
            "metric_definition_ids": [definition.definition_id],
            "metric_definition_hashes": [definition.content_hash],
            "metric_definition_versions": [definition.definition_version],
            "split_information": {"split": "test"},
            "metrics": [_mapping(package.metrics[0])],
            "artifact_references": [package.artifacts[0].artifact_id],
            "comparison_tables": [],
            "slices": {},
            "pareto_result": {},
            "findings": [_mapping(item) for item in finding],
            "evidence": [_mapping(evidence)],
            "requirements": [
                {
                    "evidence_type": evidence.evidence_type,
                    "blocking": True,
                    "minimum_status": "PASS",
                }
            ],
            "contamination_flags": [],
            "reproducibility_status": "PASS",
            "registry_integrity_status": "PASS",
            "known_limitations": [],
        }
    )
    assert result.registry_revision == 1
    assert result.completeness.status.value == "COMPLETE"
    assert result.package_hash


@pytest.mark.parametrize(
    "call",
    [
        lambda: profile({}),
        lambda: comparison_context([], "context"),
        lambda: run_slices({"dimension": "INVALID", "trades": []}),
        lambda: run_pareto(
            {
                "metric_definitions": [],
                "objective_metrics": [],
                "candidates": [],
                "missing_policy": "INVALID",
            }
        ),
        lambda: run_findings({"stability": {}, "statistical_strength": {}}),
        lambda: finding_from_mapping({}),
    ],
)
def test_cli_analytics_rejects_invalid_documents(call) -> None:
    with pytest.raises((ValidationError, ValueError)):
        call()
