from __future__ import annotations

from typing import Any, Mapping

from . import cli
from .compatibility import ComparisonContext, check_compatibility, compare_metrics_with_definitions
from .evidence import (
    EvidenceRequirement,
    StrategyAuditPackageInput,
    build_strategy_audit_package,
    evaluate_evidence_completeness,
)
from .findings import (
    StabilityInput,
    StatisticalStrengthInput,
    evaluate_stability,
    evaluate_statistical_strength,
)
from .intake import ProducerProfile
from .models import EvidenceStatus, Finding, FindingCode, MissingMetricPolicy, Severity
from .pareto import (
    ConstraintResult,
    ParetoCandidate,
    objectives_from_metric_definitions,
    rank_pareto,
)
from .serialization import (
    evidence_from_mapping,
    metric_definition_from_mapping,
    metric_from_mapping,
)
from .slices import SliceDimension, TradeFact, build_slice


def profile(document: Mapping[str, Any]) -> ProducerProfile:
    return ProducerProfile(
        producer_module_id=cli._string(document.get("producer_module_id"), "producer_module_id"),
        allowed_producer_schema_versions=frozenset(
            cli._string(item, "allowed producer schema")
            for item in cli._list(
                document.get("allowed_producer_schema_versions"),
                "allowed_producer_schema_versions",
            )
        ),
        allowed_artifact_schema_prefixes=tuple(
            cli._string(item, "allowed artifact schema prefix")
            for item in cli._list(
                document.get("allowed_artifact_schema_prefixes"),
                "allowed_artifact_schema_prefixes",
            )
        ),
        required_artifact_paths=frozenset(
            cli._string(item, "required artifact path")
            for item in cli._list(
                document.get("required_artifact_paths", []),
                "required_artifact_paths",
            )
        ),
        allowed_package_schema_versions=frozenset(
            cli._string(item, "allowed package schema")
            for item in cli._list(
                document.get("allowed_package_schema_versions", ["1"]),
                "allowed_package_schema_versions",
            )
        ),
    )


def comparison_context(value: Any, name: str) -> ComparisonContext:
    document = cli._mapping(value, name)
    return ComparisonContext(
        market=cli._string(document.get("market"), f"{name}.market"),
        instrument=cli._string(document.get("instrument"), f"{name}.instrument"),
        timeframe=cli._string(document.get("timeframe"), f"{name}.timeframe"),
        period_start=cli._string(document.get("period_start"), f"{name}.period_start"),
        period_end=cli._string(document.get("period_end"), f"{name}.period_end"),
        dataset_hashes=tuple(
            cli._string(item, f"{name}.dataset_hash")
            for item in cli._list(document.get("dataset_hashes"), f"{name}.dataset_hashes")
        ),
        split_id=cli._string(document.get("split_id"), f"{name}.split_id"),
        execution_model=cli._string(document.get("execution_model"), f"{name}.execution_model"),
        commission_model=cli._string(document.get("commission_model"), f"{name}.commission_model"),
        slippage_model=cli._string(document.get("slippage_model"), f"{name}.slippage_model"),
        funding_model=cli._string(document.get("funding_model"), f"{name}.funding_model"),
        risk_setting=cli._string(document.get("risk_setting"), f"{name}.risk_setting"),
        metric_definition_version=cli._string(
            document.get("metric_definition_version"),
            f"{name}.metric_definition_version",
        ),
        strategy_scope=cli._string(document.get("strategy_scope"), f"{name}.strategy_scope"),
        currency=cli._string(document.get("currency"), f"{name}.currency"),
        compounding_mode=cli._string(document.get("compounding_mode"), f"{name}.compounding_mode"),
    )


def run_compare(document: Mapping[str, Any]) -> Any:
    baseline_context = comparison_context(document.get("baseline_context"), "baseline_context")
    candidate_context = comparison_context(document.get("candidate_context"), "candidate_context")
    partial_fields = frozenset(
        cli._string(item, "partially comparable field")
        for item in cli._list(
            document.get("partially_comparable_fields", []),
            "partially_comparable_fields",
        )
    )
    compatibility = check_compatibility(
        baseline_context,
        candidate_context,
        partially_comparable_fields=partial_fields,
    )
    baseline_metrics = tuple(
        metric_from_mapping(cli._mapping(item, "baseline metric"))
        for item in cli._list(document.get("baseline_metrics"), "baseline_metrics")
    )
    candidate_metrics = tuple(
        metric_from_mapping(cli._mapping(item, "candidate metric"))
        for item in cli._list(document.get("candidate_metrics"), "candidate_metrics")
    )
    baseline_definitions = tuple(
        metric_definition_from_mapping(cli._mapping(item, "baseline metric definition"))
        for item in cli._list(
            document.get("baseline_metric_definitions"),
            "baseline_metric_definitions",
        )
    )
    candidate_definitions = tuple(
        metric_definition_from_mapping(cli._mapping(item, "candidate metric definition"))
        for item in cli._list(
            document.get("candidate_metric_definitions"),
            "candidate_metric_definitions",
        )
    )
    return compare_metrics_with_definitions(
        baseline_metrics,
        candidate_metrics,
        compatibility,
        baseline_definitions=baseline_definitions,
        candidate_definitions=candidate_definitions,
    )


def run_slices(document: Mapping[str, Any]) -> Any:
    dimension = SliceDimension(cli._string(document.get("dimension"), "dimension"))
    trades: list[TradeFact] = []
    for raw in cli._list(document.get("trades"), "trades"):
        item = cli._mapping(raw, "trade")
        trades.append(
            TradeFact(
                trade_id=cli._string(item.get("trade_id"), "trade_id"),
                run_id=cli._string(item.get("run_id"), "run_id"),
                instrument=cli._string(item.get("instrument"), "instrument"),
                side=cli._string(item.get("side"), "side"),
                entry_time=cli._datetime(item.get("entry_time"), "entry_time"),
                exit_time=cli._datetime(item.get("exit_time"), "exit_time"),
                net_pnl=cli._decimal(item.get("net_pnl"), "net_pnl"),
                fees=item.get("fees", 0),
                r_multiple=item.get("r_multiple"),
                module=cli._optional_string(item.get("module"), "module"),
                setup=cli._optional_string(item.get("setup"), "setup"),
                regime=cli._optional_string(item.get("regime"), "regime"),
                exit_reason=cli._optional_string(item.get("exit_reason"), "exit_reason"),
                source_artifact_hash=cli._optional_string(
                    item.get("source_artifact_hash"), "source_artifact_hash"
                ),
            )
        )
    return build_slice(trades, dimension)


def run_pareto(document: Mapping[str, Any]) -> Any:
    definitions = tuple(
        metric_definition_from_mapping(cli._mapping(item, "metric definition"))
        for item in cli._list(document.get("metric_definitions"), "metric_definitions")
    )
    metric_names = tuple(
        cli._string(item, "metric name")
        for item in cli._list(document.get("objective_metrics"), "objective_metrics")
    )
    epsilons_raw = cli._mapping(document.get("epsilons", {}), "epsilons")
    objectives = objectives_from_metric_definitions(
        definitions,
        metric_names=metric_names,
        epsilons={
            key: cli._decimal(value, f"epsilon.{key}") for key, value in epsilons_raw.items()
        },
    )
    candidates: list[ParetoCandidate] = []
    for raw in cli._list(document.get("candidates"), "candidates"):
        item = cli._mapping(raw, "candidate")
        constraints = tuple(
            ConstraintResult(
                name=cli._string(constraint.get("name"), "constraint.name"),
                passed=cli._boolean(constraint.get("passed"), "constraint.passed"),
                blocking=cli._boolean(constraint.get("blocking"), "constraint.blocking"),
                actual_value=constraint.get("actual_value"),
                threshold=constraint.get("threshold"),
                reason=cli._string(
                    constraint.get("reason", ""),
                    "constraint.reason",
                    allow_empty=True,
                ),
            )
            for constraint in (
                cli._mapping(value, "constraint")
                for value in cli._list(item.get("constraints", []), "constraints")
            )
        )
        candidates.append(
            ParetoCandidate(
                candidate_id=cli._string(item.get("candidate_id"), "candidate_id"),
                metrics=cli._mapping(item.get("metrics"), "candidate.metrics"),
                constraints=constraints,
                warnings_count=cli._nonnegative_integer(
                    item.get("warnings_count", 0), "warnings_count"
                ),
                oos_strength=cli._decimal(item.get("oos_strength", 0), "oos_strength"),
                robustness_strength=cli._decimal(
                    item.get("robustness_strength", 0), "robustness_strength"
                ),
                parameter_sensitivity=cli._decimal(
                    item.get("parameter_sensitivity", 0), "parameter_sensitivity"
                ),
                complexity=cli._decimal(item.get("complexity", 0), "complexity"),
                trade_count=cli._nonnegative_integer(item.get("trade_count", 0), "trade_count"),
                configuration_hash=cli._string(
                    item.get("configuration_hash"), "configuration_hash"
                ),
            )
        )
    policy = MissingMetricPolicy(
        cli._string(document.get("missing_policy", "EXCLUDE"), "missing_policy")
    )
    return rank_pareto(tuple(candidates), objectives, missing_policy=policy)


def run_findings(document: Mapping[str, Any]) -> Mapping[str, Any]:
    stability_raw = cli._mapping(document.get("stability"), "stability")
    statistical_raw = cli._mapping(document.get("statistical_strength"), "statistical_strength")
    stability = StabilityInput(
        candidate_id=cli._string(stability_raw.get("candidate_id"), "candidate_id"),
        period_contributions=cli._mapping(
            stability_raw.get("period_contributions", {}), "period_contributions"
        ),
        instrument_contributions=cli._mapping(
            stability_raw.get("instrument_contributions", {}),
            "instrument_contributions",
        ),
        side_contributions=cli._mapping(
            stability_raw.get("side_contributions", {}), "side_contributions"
        ),
        module_contributions=cli._mapping(
            stability_raw.get("module_contributions", {}), "module_contributions"
        ),
        regime_contributions=cli._mapping(
            stability_raw.get("regime_contributions", {}), "regime_contributions"
        ),
        trade_pnls=tuple(cli._list(stability_raw.get("trade_pnls", []), "trade_pnls")),
        oos_degradation=stability_raw.get("oos_degradation"),
        cost_degradation=stability_raw.get("cost_degradation"),
        walk_forward_dispersion=stability_raw.get("walk_forward_dispersion"),
        seed_dispersion=stability_raw.get("seed_dispersion"),
        parameter_dispersion=stability_raw.get("parameter_dispersion"),
        robustness_completed=cli._boolean(
            stability_raw.get("robustness_completed", False),
            "robustness_completed",
        ),
    )
    statistical = StatisticalStrengthInput(
        candidate_id=cli._string(statistical_raw.get("candidate_id"), "candidate_id"),
        trade_count=cli._nonnegative_integer(statistical_raw.get("trade_count"), "trade_count"),
        oos_trade_count=cli._nonnegative_integer(
            statistical_raw.get("oos_trade_count"), "oos_trade_count"
        ),
        period_days=cli._nonnegative_integer(statistical_raw.get("period_days"), "period_days"),
        independent_regimes=cli._nonnegative_integer(
            statistical_raw.get("independent_regimes"), "independent_regimes"
        ),
        confidence_interval_width=statistical_raw.get("confidence_interval_width"),
        bootstrap_present=cli._boolean(
            statistical_raw.get("bootstrap_present"), "bootstrap_present"
        ),
        monte_carlo_present=cli._boolean(
            statistical_raw.get("monte_carlo_present"), "monte_carlo_present"
        ),
        untouched_test_present=cli._boolean(
            statistical_raw.get("untouched_test_present"),
            "untouched_test_present",
        ),
        contaminated=cli._boolean(statistical_raw.get("contaminated"), "contaminated"),
    )
    return {
        "stability_findings": evaluate_stability(stability),
        "statistical_findings": evaluate_statistical_strength(statistical),
    }


def finding_from_mapping(raw: Mapping[str, Any]) -> Finding:
    return Finding(
        finding_id=cli._string(raw.get("finding_id"), "finding_id"),
        code=FindingCode(cli._string(raw.get("code"), "code")),
        severity=Severity(cli._string(raw.get("severity"), "severity")),
        blocking=cli._boolean(raw.get("blocking"), "blocking"),
        affected_scope=cli._mapping(raw.get("affected_scope"), "affected_scope"),
        thresholds=cli._mapping(raw.get("thresholds"), "thresholds"),
        actual_values=cli._mapping(raw.get("actual_values"), "actual_values"),
        evidence_ids=tuple(
            cli._string(item, "evidence_id")
            for item in cli._list(raw.get("evidence_ids", []), "evidence_ids")
        ),
        recommendation=cli._string(raw.get("recommendation"), "recommendation"),
    )


def build_audit_package(document: Mapping[str, Any]) -> Any:
    evidence = tuple(
        evidence_from_mapping(cli._mapping(item, "evidence"))
        for item in cli._list(document.get("evidence"), "evidence")
    )
    requirements = tuple(
        EvidenceRequirement(
            evidence_type=cli._string(item.get("evidence_type"), "requirement.evidence_type"),
            blocking=cli._boolean(item.get("blocking", True), "requirement.blocking"),
            minimum_status=EvidenceStatus(
                cli._string(item.get("minimum_status", "PASS"), "requirement.minimum_status")
            ),
        )
        for item in (
            cli._mapping(raw, "requirement")
            for raw in cli._list(document.get("requirements"), "requirements")
        )
    )
    completeness = evaluate_evidence_completeness(evidence, requirements)
    data = StrategyAuditPackageInput(
        registry_revision=cli._nonnegative_integer(
            document.get("registry_revision"), "registry_revision"
        ),
        registry_state_hash=cli._string(document.get("registry_state_hash"), "registry_state_hash"),
        strategy_id=cli._string(document.get("strategy_id"), "strategy_id"),
        baseline_ids=tuple(
            cli._string(item, "baseline_id")
            for item in cli._list(document.get("baseline_ids", []), "baseline_ids")
        ),
        candidate_ids=tuple(
            cli._string(item, "candidate_id")
            for item in cli._list(document.get("candidate_ids", []), "candidate_ids")
        ),
        experiment_ids=tuple(
            cli._string(item, "experiment_id")
            for item in cli._list(document.get("experiment_ids", []), "experiment_ids")
        ),
        run_ids=tuple(
            cli._string(item, "run_id")
            for item in cli._list(document.get("run_ids", []), "run_ids")
        ),
        configuration_ids=tuple(
            cli._string(item, "configuration_id")
            for item in cli._list(document.get("configuration_ids", []), "configuration_ids")
        ),
        dataset_ids=tuple(
            cli._string(item, "dataset_id")
            for item in cli._list(document.get("dataset_ids", []), "dataset_ids")
        ),
        dataset_hashes=tuple(
            cli._string(item, "dataset_hash")
            for item in cli._list(document.get("dataset_hashes", []), "dataset_hashes")
        ),
        commit_shas=tuple(
            cli._string(item, "commit_sha")
            for item in cli._list(document.get("commit_shas", []), "commit_shas")
        ),
        module_versions={
            cli._string(key, "module name"): cli._string(value, "module version")
            for key, value in cli._mapping(
                document.get("module_versions", {}), "module_versions"
            ).items()
        },
        backtest_engine_versions=tuple(
            cli._string(item, "backtest engine version")
            for item in cli._list(
                document.get("backtest_engine_versions", []),
                "backtest_engine_versions",
            )
        ),
        metric_definition_ids=tuple(
            cli._string(item, "metric definition ID")
            for item in cli._list(
                document.get("metric_definition_ids", []),
                "metric_definition_ids",
            )
        ),
        metric_definition_hashes=tuple(
            cli._string(item, "metric definition hash")
            for item in cli._list(
                document.get("metric_definition_hashes", []),
                "metric_definition_hashes",
            )
        ),
        metric_definition_versions=tuple(
            cli._string(item, "metric definition version")
            for item in cli._list(
                document.get("metric_definition_versions", []),
                "metric_definition_versions",
            )
        ),
        split_information=cli._mapping(document.get("split_information", {}), "split_information"),
        metrics=tuple(
            cli._mapping(item, "metric")
            for item in cli._list(document.get("metrics", []), "metrics")
        ),
        artifact_references=tuple(
            cli._string(item, "artifact reference")
            for item in cli._list(document.get("artifact_references", []), "artifact_references")
        ),
        comparison_tables=tuple(
            cli._mapping(item, "comparison table")
            for item in cli._list(document.get("comparison_tables", []), "comparison_tables")
        ),
        slices={
            cli._string(key, "slice key"): tuple(
                cli._mapping(item, "slice item") for item in cli._list(value, f"slices.{key}")
            )
            for key, value in cli._mapping(document.get("slices", {}), "slices").items()
        },
        pareto_result=cli._mapping(document.get("pareto_result", {}), "pareto_result"),
        findings=tuple(
            finding_from_mapping(cli._mapping(item, "finding"))
            for item in cli._list(document.get("findings", []), "findings")
        ),
        evidence=evidence,
        completeness=completeness,
        contamination_flags=tuple(
            cli._string(item, "contamination flag")
            for item in cli._list(document.get("contamination_flags", []), "contamination_flags")
        ),
        reproducibility_status=cli._string(
            document.get("reproducibility_status"), "reproducibility_status"
        ),
        registry_integrity_status=cli._string(
            document.get("registry_integrity_status"),
            "registry_integrity_status",
        ),
        known_limitations=tuple(
            cli._string(item, "known limitation")
            for item in cli._list(document.get("known_limitations", []), "known_limitations")
        ),
    )
    return build_strategy_audit_package(data)
