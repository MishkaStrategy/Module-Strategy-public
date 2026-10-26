"""Strict decoders for versioned Strategy Factory JSON artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .contracts import (
    AdaptationPolicy,
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    DatasetDescriptor,
    Direction,
    ExperimentPlan,
    FactoryMode,
    FactoryStage,
    HoldoutReport,
    ImprovementPolicy,
    InstrumentProfile,
    MarketRegimeDefinition,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    ParameterSpace,
    ResourceBudget,
    RobustnessReport,
    StrategyArchitecture,
    StrategyAuditDecision,
    StrategyFactoryManifest,
    StrategyFactoryRequest,
    StrategyHypothesis,
    StrategyPackage,
    TimeRange,
)
from .errors import ContractError


def decode_strategy_factory_request(value: object) -> StrategyFactoryRequest:
    data = _strict_mapping(
        value,
        {
            "request_id",
            "mode",
            "symbol",
            "exchange",
            "market_type",
            "dataset",
            "allowed_timeframes",
            "initial_capital",
            "research_risk_fraction",
            "maximum_drawdown_fraction",
            "minimum_trade_count",
            "objectives",
            "budget",
            "random_seed",
            "allowed_strategy_families",
            "forbidden_module_ids",
            "baseline_strategy_id",
            "source_strategy_id",
            "adaptation_policy",
            "improvement_policy",
            "explicit_holdout",
            "created_at",
            "schema_version",
        },
        "StrategyFactoryRequest",
    )
    dataset = _strict_mapping(
        data["dataset"],
        {
            "dataset_id",
            "source",
            "symbol",
            "exchange",
            "market_type",
            "base_timeframe",
            "available_timeframes",
            "period",
            "timezone",
            "file_format",
            "uri",
            "sha256",
            "row_count",
            "received_at",
            "schema_version",
            "funding_uri",
            "funding_sha256",
            "exchange_info_sha256",
        },
        "DatasetDescriptor",
    )
    period = _strict_mapping(dataset["period"], {"start", "end"}, "dataset period")
    budget = _strict_mapping(
        data["budget"],
        {
            "maximum_experiments",
            "maximum_architectures",
            "maximum_finalists",
            "maximum_duration_seconds",
            "maximum_memory_bytes",
            "maximum_cpu_workers",
            "maximum_parallel_tasks",
            "stage_experiment_limits",
            "schema_version",
        },
        "ResourceBudget",
    )
    adaptation = _strict_mapping(
        data["adaptation_policy"],
        {
            "allow_parameter_adaptation",
            "allowed_parameter_names",
            "neighborhood_points",
            "require_source_baseline_control",
            "allow_direction_reduction",
            "schema_version",
        },
        "AdaptationPolicy",
    )
    improvement = _strict_mapping(
        data["improvement_policy"],
        {
            "allowed_mutations",
            "allowed_parameter_names",
            "neighborhood_points",
            "maximum_runtime_module_changes",
            "maximum_architectures",
            "maximum_complexity_increase",
            "require_baseline_control",
            "schema_version",
        },
        "ImprovementPolicy",
    )
    holdout_raw = data["explicit_holdout"]
    holdout = None
    if holdout_raw is not None:
        holdout_mapping = _strict_mapping(holdout_raw, {"start", "end"}, "explicit_holdout")
        holdout = TimeRange(
            start=_datetime(holdout_mapping["start"], "holdout start"),
            end=_datetime(holdout_mapping["end"], "holdout end"),
        )
    return StrategyFactoryRequest(
        request_id=_text(data["request_id"], "request_id"),
        mode=_enum(FactoryMode, data["mode"], "mode"),
        symbol=_text(data["symbol"], "symbol"),
        exchange=_text(data["exchange"], "exchange"),
        market_type=_enum(MarketType, data["market_type"], "market_type"),
        dataset=DatasetDescriptor(
            dataset_id=_text(dataset["dataset_id"], "dataset_id"),
            source=_text(dataset["source"], "dataset source"),
            symbol=_text(dataset["symbol"], "dataset symbol"),
            exchange=_text(dataset["exchange"], "dataset exchange"),
            market_type=_enum(MarketType, dataset["market_type"], "dataset market_type"),
            base_timeframe=_text(dataset["base_timeframe"], "base_timeframe"),
            available_timeframes=_string_tuple(
                dataset["available_timeframes"], "available_timeframes"
            ),
            period=TimeRange(
                start=_datetime(period["start"], "period start"),
                end=_datetime(period["end"], "period end"),
            ),
            timezone=_text(dataset["timezone"], "timezone"),
            file_format=_text(dataset["file_format"], "file_format"),
            uri=_text(dataset["uri"], "uri"),
            sha256=_text(dataset["sha256"], "dataset sha256"),
            row_count=_int(dataset["row_count"], "row_count"),
            received_at=_datetime(dataset["received_at"], "received_at"),
            schema_version=_int(dataset["schema_version"], "dataset schema_version"),
            funding_uri=_optional_text(dataset["funding_uri"], "funding_uri"),
            funding_sha256=_optional_text(dataset["funding_sha256"], "funding_sha256"),
            exchange_info_sha256=_optional_text(
                dataset["exchange_info_sha256"], "exchange_info_sha256"
            ),
        ),
        allowed_timeframes=_string_tuple(data["allowed_timeframes"], "allowed_timeframes"),
        initial_capital=_decimal(data["initial_capital"], "initial_capital"),
        research_risk_fraction=_decimal(data["research_risk_fraction"], "research_risk_fraction"),
        maximum_drawdown_fraction=_decimal(
            data["maximum_drawdown_fraction"], "maximum_drawdown_fraction"
        ),
        minimum_trade_count=_int(data["minimum_trade_count"], "minimum_trade_count"),
        objectives=_mapping_value(data["objectives"], "objectives"),
        budget=ResourceBudget(
            maximum_experiments=_int(budget["maximum_experiments"], "maximum_experiments"),
            maximum_architectures=_int(budget["maximum_architectures"], "maximum_architectures"),
            maximum_finalists=_int(budget["maximum_finalists"], "maximum_finalists"),
            maximum_duration_seconds=_int(
                budget["maximum_duration_seconds"], "maximum_duration_seconds"
            ),
            maximum_memory_bytes=_int(budget["maximum_memory_bytes"], "maximum_memory_bytes"),
            maximum_cpu_workers=_int(budget["maximum_cpu_workers"], "maximum_cpu_workers"),
            maximum_parallel_tasks=_int(budget["maximum_parallel_tasks"], "maximum_parallel_tasks"),
            stage_experiment_limits=_mapping_value(
                budget["stage_experiment_limits"], "stage_experiment_limits"
            ),
            schema_version=_int(budget["schema_version"], "budget schema_version"),
        ),
        random_seed=_int(data["random_seed"], "random_seed"),
        allowed_strategy_families=frozenset(
            _string_tuple(data["allowed_strategy_families"], "allowed_strategy_families")
        ),
        forbidden_module_ids=frozenset(
            _string_tuple(data["forbidden_module_ids"], "forbidden_module_ids")
        ),
        baseline_strategy_id=_optional_text(data["baseline_strategy_id"], "baseline_strategy_id"),
        source_strategy_id=_optional_text(data["source_strategy_id"], "source_strategy_id"),
        adaptation_policy=AdaptationPolicy(
            allow_parameter_adaptation=_bool(
                adaptation["allow_parameter_adaptation"], "allow_parameter_adaptation"
            ),
            allowed_parameter_names=frozenset(
                _string_tuple(adaptation["allowed_parameter_names"], "allowed_parameter_names")
            ),
            neighborhood_points=_int(adaptation["neighborhood_points"], "neighborhood_points"),
            require_source_baseline_control=_bool(
                adaptation["require_source_baseline_control"], "require_source_baseline_control"
            ),
            allow_direction_reduction=_bool(
                adaptation["allow_direction_reduction"], "allow_direction_reduction"
            ),
            schema_version=_int(adaptation["schema_version"], "adaptation schema_version"),
        ),
        improvement_policy=ImprovementPolicy(
            allowed_mutations=frozenset(
                _string_tuple(improvement["allowed_mutations"], "allowed_mutations")
            ),
            allowed_parameter_names=frozenset(
                _string_tuple(
                    improvement["allowed_parameter_names"], "improvement allowed_parameter_names"
                )
            ),
            neighborhood_points=_int(
                improvement["neighborhood_points"], "improvement neighborhood_points"
            ),
            maximum_runtime_module_changes=_int(
                improvement["maximum_runtime_module_changes"], "maximum_runtime_module_changes"
            ),
            maximum_architectures=_int(
                improvement["maximum_architectures"], "improvement maximum_architectures"
            ),
            maximum_complexity_increase=_decimal(
                improvement["maximum_complexity_increase"], "maximum_complexity_increase"
            ),
            require_baseline_control=_bool(
                improvement["require_baseline_control"], "require_baseline_control"
            ),
            schema_version=_int(improvement["schema_version"], "improvement schema_version"),
        ),
        explicit_holdout=holdout,
        created_at=_datetime(data["created_at"], "created_at"),
        schema_version=_int(data["schema_version"], "request schema_version"),
    )


def decode_strategy_factory_manifest(value: object) -> StrategyFactoryManifest:
    data = _strict_mapping(
        value,
        {
            "manifest_id",
            "run_id",
            "request_fingerprint",
            "dataset_fingerprint",
            "commit_sha",
            "core_version",
            "research_runner_version",
            "results_registry_version",
            "strategy_factory_version",
            "module_versions",
            "random_seed",
            "created_at",
            "schema_version",
        },
        "StrategyFactoryManifest",
    )
    return StrategyFactoryManifest(
        manifest_id=_text(data["manifest_id"], "manifest_id"),
        run_id=_text(data["run_id"], "run_id"),
        request_fingerprint=_text(data["request_fingerprint"], "request_fingerprint"),
        dataset_fingerprint=_text(data["dataset_fingerprint"], "dataset_fingerprint"),
        commit_sha=_text(data["commit_sha"], "commit_sha"),
        core_version=_text(data["core_version"], "core_version"),
        research_runner_version=_text(data["research_runner_version"], "research_runner_version"),
        results_registry_version=_text(
            data["results_registry_version"], "results_registry_version"
        ),
        strategy_factory_version=_text(
            data["strategy_factory_version"], "strategy_factory_version"
        ),
        module_versions=_mapping_value(data["module_versions"], "module_versions"),
        random_seed=_int(data["random_seed"], "random_seed"),
        created_at=_datetime(data["created_at"], "created_at"),
        schema_version=_int(data["schema_version"], "manifest schema_version"),
    )


def decode_instrument_profile(value: object) -> InstrumentProfile:
    data = _strict_mapping(
        value,
        {
            "profile_id",
            "dataset_fingerprint",
            "symbol",
            "market_type",
            "generated_at",
            "history_days",
            "observations",
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
            "warnings",
            "schema_version",
        },
        "InstrumentProfile",
    )
    return InstrumentProfile(
        profile_id=_text(data["profile_id"], "profile_id"),
        dataset_fingerprint=_text(data["dataset_fingerprint"], "dataset_fingerprint"),
        symbol=_text(data["symbol"], "symbol"),
        market_type=_enum(MarketType, data["market_type"], "market_type"),
        generated_at=_datetime(data["generated_at"], "generated_at"),
        history_days=_decimal(data["history_days"], "history_days"),
        observations=_int(data["observations"], "observations"),
        liquidity_metrics=_mapping_value(data["liquidity_metrics"], "liquidity_metrics"),
        volatility_metrics=_mapping_value(data["volatility_metrics"], "volatility_metrics"),
        return_distribution=_mapping_value(data["return_distribution"], "return_distribution"),
        trend_metrics=_mapping_value(data["trend_metrics"], "trend_metrics"),
        mean_reversion_metrics=_mapping_value(
            data["mean_reversion_metrics"], "mean_reversion_metrics"
        ),
        seasonality_metrics=_mapping_value(data["seasonality_metrics"], "seasonality_metrics"),
        tail_risk_metrics=_mapping_value(data["tail_risk_metrics"], "tail_risk_metrics"),
        funding_metrics=_mapping_value(data["funding_metrics"], "funding_metrics"),
        cost_sensitivity=_mapping_value(data["cost_sensitivity"], "cost_sensitivity"),
        stability_metrics=_mapping_value(data["stability_metrics"], "stability_metrics"),
        warnings=_string_tuple(data["warnings"], "warnings"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_market_regime_definition(value: object) -> MarketRegimeDefinition:
    data = _strict_mapping(
        value,
        {
            "regime_id",
            "name",
            "description",
            "feature_version",
            "point_in_time",
            "required_timeframes",
            "parameters",
            "schema_version",
        },
        "MarketRegimeDefinition",
    )
    return MarketRegimeDefinition(
        regime_id=_text(data["regime_id"], "regime_id"),
        name=_text(data["name"], "name"),
        description=_text(data["description"], "description"),
        feature_version=_text(data["feature_version"], "feature_version"),
        point_in_time=_bool(data["point_in_time"], "point_in_time"),
        required_timeframes=_string_tuple(data["required_timeframes"], "required_timeframes"),
        parameters=_mapping_value(data["parameters"], "parameters"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_module_reference(value: object) -> ModuleReference:
    data = _strict_mapping(
        value,
        {
            "module_id",
            "version",
            "category",
            "import_path",
            "api_version",
            "state_schema_version",
            "required_timeframes",
            "required_features",
            "produced_features",
            "required_data",
            "supported_markets",
            "supported_directions",
            "deterministic",
            "test_coverage",
            "maturity",
            "optional_features",
            "fixed_parameters",
            "parameter_search_hints",
            "evidence_links",
            "source_commit_sha",
            "compatibility_tags",
            "conflict_tags",
            "parameter_schema",
            "runtime_component_id",
            "runtime_component_version",
            "runtime_role_categories",
            "role_facet",
            "schema_version",
        },
        "ModuleReference",
    )
    return ModuleReference(
        module_id=_text(data["module_id"], "module_id"),
        version=_text(data["version"], "version"),
        category=_enum(ModuleCategory, data["category"], "category"),
        import_path=_text(data["import_path"], "import_path"),
        api_version=_text(data["api_version"], "api_version"),
        state_schema_version=_text(data["state_schema_version"], "state_schema_version"),
        required_timeframes=_string_tuple(data["required_timeframes"], "required_timeframes"),
        required_features=_string_tuple(data["required_features"], "required_features"),
        produced_features=_string_tuple(data["produced_features"], "produced_features"),
        required_data=frozenset(_string_tuple(data["required_data"], "required_data")),
        supported_markets=frozenset(
            _enum_sequence(MarketType, data["supported_markets"], "supported_markets")
        ),
        supported_directions=frozenset(
            _enum_sequence(Direction, data["supported_directions"], "supported_directions")
        ),
        deterministic=_bool(data["deterministic"], "deterministic"),
        test_coverage=_decimal(data["test_coverage"], "test_coverage"),
        maturity=_enum(ModuleMaturity, data["maturity"], "maturity"),
        optional_features=_string_tuple(data["optional_features"], "optional_features"),
        fixed_parameters=_mapping_value(data["fixed_parameters"], "fixed_parameters"),
        parameter_search_hints=_mapping_value(
            data["parameter_search_hints"], "parameter_search_hints"
        ),
        evidence_links=_string_tuple(data["evidence_links"], "evidence_links"),
        source_commit_sha=_optional_text(data["source_commit_sha"], "source_commit_sha"),
        compatibility_tags=frozenset(
            _string_tuple(data["compatibility_tags"], "compatibility_tags")
        ),
        conflict_tags=frozenset(_string_tuple(data["conflict_tags"], "conflict_tags")),
        parameter_schema=_mapping_value(data["parameter_schema"], "parameter_schema"),
        runtime_component_id=_optional_text(data["runtime_component_id"], "runtime_component_id"),
        runtime_component_version=_optional_text(
            data["runtime_component_version"], "runtime_component_version"
        ),
        runtime_role_categories=frozenset(
            _enum_sequence(
                ModuleCategory,
                data["runtime_role_categories"],
                "runtime_role_categories",
            )
        ),
        role_facet=_optional_text(data["role_facet"], "role_facet"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_strategy_hypothesis(value: object) -> StrategyHypothesis:
    data = _strict_mapping(
        value,
        {
            "hypothesis_id",
            "description",
            "expected_edge",
            "applicability",
            "risks",
            "module_candidates",
            "parameter_ranges",
            "falsification_criteria",
            "estimated_experiments",
            "novelty",
            "origin",
            "schema_version",
        },
        "StrategyHypothesis",
    )
    return StrategyHypothesis(
        hypothesis_id=_text(data["hypothesis_id"], "hypothesis_id"),
        description=_text(data["description"], "description"),
        expected_edge=_text(data["expected_edge"], "expected_edge"),
        applicability=_string_tuple(data["applicability"], "applicability"),
        risks=_string_tuple(data["risks"], "risks"),
        module_candidates=_decoded_tuple(
            data["module_candidates"], decode_module_reference, "module_candidates"
        ),
        parameter_ranges=_mapping_value(data["parameter_ranges"], "parameter_ranges"),
        falsification_criteria=_mapping_value(
            data["falsification_criteria"], "falsification_criteria"
        ),
        estimated_experiments=_int(data["estimated_experiments"], "estimated_experiments"),
        novelty=_decimal(data["novelty"], "novelty"),
        origin=_mapping_value(data["origin"], "origin"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_strategy_architecture(value: object) -> StrategyArchitecture:
    data = _strict_mapping(
        value,
        {
            "architecture_id",
            "market_type",
            "directions",
            "signal",
            "context_filters",
            "entry",
            "protection",
            "target",
            "risk",
            "setup_adapters",
            "risk_adapters",
            "position_management",
            "portfolio_constraints",
            "parameters",
            "grammar_version",
            "schema_version",
        },
        "StrategyArchitecture",
    )
    return StrategyArchitecture(
        architecture_id=_text(data["architecture_id"], "architecture_id"),
        market_type=_enum(MarketType, data["market_type"], "market_type"),
        directions=frozenset(_enum_sequence(Direction, data["directions"], "directions")),
        signal=decode_module_reference(data["signal"]),
        context_filters=_decoded_tuple(
            data["context_filters"], decode_module_reference, "context_filters"
        ),
        entry=decode_module_reference(data["entry"]),
        protection=decode_module_reference(data["protection"]),
        target=decode_module_reference(data["target"]),
        risk=decode_module_reference(data["risk"]),
        setup_adapters=_decoded_tuple(
            data["setup_adapters"], decode_module_reference, "setup_adapters"
        ),
        risk_adapters=_decoded_tuple(
            data["risk_adapters"], decode_module_reference, "risk_adapters"
        ),
        position_management=_decoded_tuple(
            data["position_management"],
            decode_module_reference,
            "position_management",
        ),
        portfolio_constraints=_decoded_tuple(
            data["portfolio_constraints"],
            decode_module_reference,
            "portfolio_constraints",
        ),
        parameters=_mapping_value(data["parameters"], "parameters"),
        grammar_version=_text(data["grammar_version"], "grammar_version"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_parameter_space(value: object) -> ParameterSpace:
    data = _strict_mapping(
        value,
        {
            "space_id",
            "architecture_id",
            "dimensions",
            "constraints",
            "maximum_combinations",
            "schema_version",
        },
        "ParameterSpace",
    )
    raw_dimensions = _mapping(data["dimensions"], "dimensions")
    dimensions: dict[str, tuple[Any, ...]] = {}
    for key, values in raw_dimensions.items():
        dimensions[_text(key, "dimension name")] = _value_tuple(values, f"dimension {key}")
    constraints = _sequence(data["constraints"], "constraints")
    normalized_constraints = tuple(
        _mapping_value(item, "parameter constraint") for item in constraints
    )
    return ParameterSpace(
        space_id=_text(data["space_id"], "space_id"),
        architecture_id=_text(data["architecture_id"], "architecture_id"),
        dimensions=dimensions,
        constraints=normalized_constraints,
        maximum_combinations=_int(data["maximum_combinations"], "maximum_combinations"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_experiment_plan(value: object) -> ExperimentPlan:
    data = _strict_mapping(
        value,
        {
            "plan_id",
            "factory_run_id",
            "stage",
            "architecture",
            "parameter_space",
            "dataset_fingerprint",
            "research_specification",
            "resource_limits",
            "deterministic_seed",
            "manifest_id",
            "schema_version",
        },
        "ExperimentPlan",
    )
    return ExperimentPlan(
        plan_id=_text(data["plan_id"], "plan_id"),
        factory_run_id=_text(data["factory_run_id"], "factory_run_id"),
        stage=_enum(FactoryStage, data["stage"], "stage"),
        architecture=decode_strategy_architecture(data["architecture"]),
        parameter_space=decode_parameter_space(data["parameter_space"]),
        dataset_fingerprint=_text(data["dataset_fingerprint"], "dataset_fingerprint"),
        research_specification=_mapping_value(
            data["research_specification"], "research_specification"
        ),
        resource_limits=_mapping_value(data["resource_limits"], "resource_limits"),
        deterministic_seed=_int(data["deterministic_seed"], "deterministic_seed"),
        manifest_id=_text(data["manifest_id"], "manifest_id"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_candidate_evaluation(value: object) -> CandidateEvaluation:
    data = _strict_mapping(
        value,
        {
            "evaluation_id",
            "candidate_id",
            "metrics",
            "constraints",
            "pareto_front",
            "instability_flags",
            "complexity",
            "evidence_references",
            "schema_version",
        },
        "CandidateEvaluation",
    )
    constraints = _mapping(data["constraints"], "constraints")
    normalized_constraints = {
        _text(key, "constraint name"): _bool(item, f"constraint {key}")
        for key, item in constraints.items()
    }
    return CandidateEvaluation(
        evaluation_id=_text(data["evaluation_id"], "evaluation_id"),
        candidate_id=_text(data["candidate_id"], "candidate_id"),
        metrics=_mapping_value(data["metrics"], "metrics"),
        constraints=normalized_constraints,
        pareto_front=_int(data["pareto_front"], "pareto_front"),
        instability_flags=_string_tuple(data["instability_flags"], "instability_flags"),
        complexity=_decimal(data["complexity"], "complexity"),
        evidence_references=_string_tuple(data["evidence_references"], "evidence_references"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_candidate_strategy(value: object) -> CandidateStrategy:
    data = _strict_mapping(
        value,
        {
            "candidate_id",
            "architecture",
            "parameters",
            "configuration_hash",
            "lineage_references",
            "evaluation",
            "schema_version",
        },
        "CandidateStrategy",
    )
    evaluation = data["evaluation"]
    return CandidateStrategy(
        candidate_id=_text(data["candidate_id"], "candidate_id"),
        architecture=decode_strategy_architecture(data["architecture"]),
        parameters=_mapping_value(data["parameters"], "parameters"),
        configuration_hash=_text(data["configuration_hash"], "configuration_hash"),
        lineage_references=_string_tuple(data["lineage_references"], "lineage_references"),
        evaluation=(None if evaluation is None else decode_candidate_evaluation(evaluation)),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_strategy_package(value: object) -> StrategyPackage:
    data = _strict_mapping(
        value,
        {
            "strategy_id",
            "version",
            "symbol",
            "exchange",
            "market_type",
            "directions",
            "architecture",
            "parameters",
            "risk_policy",
            "constraints",
            "timeframes",
            "execution_assumptions",
            "dataset_hashes",
            "research_periods",
            "commit_sha",
            "random_seeds",
            "key_results",
            "robustness_report",
            "holdout_report",
            "known_weaknesses",
            "disabled_regimes",
            "admission_status",
            "evidence_links",
            "paper_configuration",
            "package_hash",
            "live_trading_authorized",
            "schema_version",
        },
        "StrategyPackage",
    )
    periods_raw = _mapping(data["research_periods"], "research_periods")
    periods: dict[str, TimeRange] = {}
    for name, raw in periods_raw.items():
        period = _strict_mapping(raw, {"start", "end"}, f"research_periods.{name}")
        periods[_text(name, "research period name")] = TimeRange(
            _datetime(period["start"], f"{name}.start"),
            _datetime(period["end"], f"{name}.end"),
        )
    return StrategyPackage(
        strategy_id=_text(data["strategy_id"], "strategy_id"),
        version=_text(data["version"], "version"),
        symbol=_text(data["symbol"], "symbol"),
        exchange=_text(data["exchange"], "exchange"),
        market_type=_enum(MarketType, data["market_type"], "market_type"),
        directions=frozenset(_enum_sequence(Direction, data["directions"], "directions")),
        architecture=decode_strategy_architecture(data["architecture"]),
        parameters=_mapping_value(data["parameters"], "parameters"),
        risk_policy=_mapping_value(data["risk_policy"], "risk_policy"),
        constraints=_mapping_value(data["constraints"], "constraints"),
        timeframes=_string_tuple(data["timeframes"], "timeframes"),
        execution_assumptions=_mapping_value(
            data["execution_assumptions"], "execution_assumptions"
        ),
        dataset_hashes=_string_tuple(data["dataset_hashes"], "dataset_hashes"),
        research_periods=periods,
        commit_sha=_text(data["commit_sha"], "commit_sha"),
        random_seeds=tuple(
            _int(item, "random_seed") for item in _sequence(data["random_seeds"], "random_seeds")
        ),
        key_results=_mapping_value(data["key_results"], "key_results"),
        robustness_report=decode_robustness_report(data["robustness_report"]),
        holdout_report=decode_holdout_report(data["holdout_report"]),
        known_weaknesses=_string_tuple(data["known_weaknesses"], "known_weaknesses"),
        disabled_regimes=_string_tuple(data["disabled_regimes"], "disabled_regimes"),
        admission_status=_enum(AdmissionStatus, data["admission_status"], "admission_status"),
        evidence_links=_string_tuple(data["evidence_links"], "evidence_links"),
        paper_configuration=_mapping_value(data["paper_configuration"], "paper_configuration"),
        package_hash=_text(data["package_hash"], "package_hash"),
        live_trading_authorized=_bool(data["live_trading_authorized"], "live_trading_authorized"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_strategy_audit_decision(value: object) -> StrategyAuditDecision:
    data = _strict_mapping(
        value,
        {
            "audit_id",
            "candidate_id",
            "status",
            "evidence_package_reference",
            "evidence_hash",
            "reason_codes",
            "issued_at",
            "auditor_id",
            "contract_version",
            "signature",
            "decision_hash",
            "schema_version",
        },
        "StrategyAuditDecision",
    )
    return StrategyAuditDecision(
        audit_id=_text(data["audit_id"], "audit_id"),
        candidate_id=_text(data["candidate_id"], "candidate_id"),
        status=_enum(AdmissionStatus, data["status"], "status"),
        evidence_package_reference=_text(
            data["evidence_package_reference"], "evidence_package_reference"
        ),
        evidence_hash=_text(data["evidence_hash"], "evidence_hash"),
        reason_codes=_string_tuple(data["reason_codes"], "reason_codes"),
        issued_at=_datetime(data["issued_at"], "issued_at"),
        auditor_id=_text(data["auditor_id"], "auditor_id"),
        contract_version=_text(data["contract_version"], "contract_version"),
        signature=_text(data["signature"], "signature"),
        decision_hash=_text(data["decision_hash"], "decision_hash"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_holdout_report(value: object) -> HoldoutReport:
    data = _strict_mapping(
        value,
        {
            "report_id",
            "candidate_id",
            "period",
            "dataset_fingerprint",
            "metrics",
            "passed",
            "opened_once",
            "generated_at",
            "schema_version",
        },
        "HoldoutReport",
    )
    period = _strict_mapping(data["period"], {"start", "end"}, "TimeRange")
    return HoldoutReport(
        report_id=_text(data["report_id"], "report_id"),
        candidate_id=_text(data["candidate_id"], "candidate_id"),
        period=TimeRange(
            _datetime(period["start"], "period.start"),
            _datetime(period["end"], "period.end"),
        ),
        dataset_fingerprint=_text(data["dataset_fingerprint"], "dataset_fingerprint"),
        metrics=_mapping_value(data["metrics"], "metrics"),
        passed=_bool(data["passed"], "passed"),
        opened_once=_bool(data["opened_once"], "opened_once"),
        generated_at=_datetime(data["generated_at"], "generated_at"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def decode_robustness_report(value: object) -> RobustnessReport:
    data = _strict_mapping(
        value,
        {
            "report_id",
            "candidate_id",
            "passed",
            "scenario_results",
            "failure_reasons",
            "generated_at",
            "schema_version",
        },
        "RobustnessReport",
    )
    return RobustnessReport(
        report_id=_text(data["report_id"], "report_id"),
        candidate_id=_text(data["candidate_id"], "candidate_id"),
        passed=_bool(data["passed"], "passed"),
        scenario_results=_mapping_value(data["scenario_results"], "scenario_results"),
        failure_reasons=_string_tuple(data["failure_reasons"], "failure_reasons"),
        generated_at=_datetime(data["generated_at"], "generated_at"),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def _strict_mapping(value: object, fields: set[str], name: str) -> Mapping[str, Any]:
    data = _mapping(value, name)
    observed = set(data)
    if observed != fields:
        missing = sorted(fields - observed)
        unknown = sorted(observed - fields)
        raise ContractError(f"{name} fields are invalid; missing={missing}, unknown={unknown}")
    return data


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a mapping")
    if any(not isinstance(key, str) or not key for key in value):
        raise ContractError(f"{name} keys must be non-blank strings")
    return value


def _mapping_value(value: object, name: str) -> Mapping[str, Any]:
    return {key: _restore(item) for key, item in _mapping(value, name).items()}


def _sequence(value: object, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{name} must be a sequence")
    return value


def _decoded_tuple[T](
    value: object,
    decoder: Callable[[object], T],
    name: str,
) -> tuple[T, ...]:
    return tuple(decoder(item) for item in _sequence(value, name))


def _value_tuple(value: object, name: str) -> tuple[Any, ...]:
    return tuple(_restore(item) for item in _sequence(value, name))


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    return tuple(_text(item, name) for item in _sequence(value, name))


def _enum_sequence[T: StrEnum](enum_type: type[T], value: object, name: str) -> tuple[T, ...]:
    return tuple(_enum(enum_type, item, name) for item in _sequence(value, name))


def _restore(value: object) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"$decimal"}:
            return _decimal(value, "decimal")
        return {str(key): _restore(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_restore(item) for item in value)
    return value


def _decimal(value: object, name: str) -> Decimal:
    raw = value.get("$decimal") if isinstance(value, Mapping) else value
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ContractError(f"{name} must be decimal-compatible")
    try:
        result = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{name} is invalid") from exc
    if not result.is_finite():
        raise ContractError(f"{name} must be finite")
    return result


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} is not ISO-8601") from exc


def _enum[T: StrEnum](enum_type: type[T], value: object, name: str) -> T:
    text = _text(value, name)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise ContractError(f"{name} has unsupported value: {text}") from exc


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-blank string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{name} must be int")
    return value


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be bool")
    return value
