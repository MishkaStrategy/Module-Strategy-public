from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest
from conftest import module

import strategy_factory.contracts as contracts
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdaptationPolicy,
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    DatasetGateStatus,
    DatasetValidationReport,
    DecisionAction,
    Direction,
    ExperimentBatchRequest,
    ExperimentBatchResult,
    ExperimentPlan,
    FactoryDecision,
    FactoryMode,
    FactoryStage,
    FactoryStatus,
    HoldoutReport,
    ImprovementPolicy,
    InstrumentProfile,
    MarketRegimeDefinition,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    ParameterSpace,
    RobustnessReport,
    StrategyAuditDecision,
    StrategyFactoryCheckpoint,
    StrategyFactoryManifest,
    StrategyHypothesis,
    StrategyPackage,
    TimeRange,
)
from strategy_factory.errors import ContractError

NOW = datetime(2026, 1, 2, tzinfo=UTC)
SHA = "a" * 64
GIT = "b" * 40


@pytest.mark.parametrize(
    ("fn", "args", "message"),
    [
        (contracts._id, (None, "x"), "portable identifier"),
        (contracts._id, ("bad id", "x"), "portable identifier"),
        (contracts._text, (None, "x"), "non-empty"),
        (contracts._text, ("   ", "x"), "non-empty"),
        (contracts._sha, ("A" * 64, "x"), "SHA-256"),
        (contracts._git_sha, ("a" * 41, "x"), "Git object"),
        (contracts._utc, (datetime(2026, 1, 1), "x"), "timezone-aware"),
        (contracts._positive_int, (True, "x"), "positive integer"),
        (contracts._positive_int, (0, "x"), "positive integer"),
        (contracts._finite_decimal, (Decimal("NaN"), "x"), "finite Decimal"),
    ],
)
def test_private_contract_validators_reject_invalid_values(fn, args, message):
    with pytest.raises(ContractError, match=message):
        fn(*args)


def test_private_contract_validators_normalize_and_enforce_minimum() -> None:
    assert contracts._text("  x  ", "x") == "x"
    assert contracts._utc(datetime(2026, 1, 1, 1, tzinfo=UTC), "x").tzinfo is UTC
    assert contracts._positive_int(0, "x", allow_zero=True) == 0
    with pytest.raises(ContractError, match=">="):
        contracts._finite_decimal(Decimal("-1"), "x", minimum=Decimal("0"))


@pytest.mark.parametrize("value", [True, object(), "nan", "inf"])
def test_numeric_decimal_rejects_non_numeric_or_nonfinite(value) -> None:
    with pytest.raises(ContractError, match="numeric|finite"):
        contracts._numeric_decimal(value, "value")


@pytest.mark.parametrize(
    ("value", "schema", "message"),
    [
        (True, {"type": "integer"}, "integer"),
        (object(), {"type": "number"}, "numeric"),
        (1, {"type": "boolean"}, "boolean"),
        (1, {"type": "string"}, "string"),
        (1, {"type": "array"}, "array"),
        ([], {"type": "object"}, "object"),
        ("x", {"enum": ["y"]}, "enum"),
        (0, {"minimum": 1}, "minimum"),
        (2, {"maximum": 1}, "maximum"),
    ],
)
def test_parameter_value_validation(value, schema, message) -> None:
    with pytest.raises(ContractError, match=message):
        contracts._validate_parameter_value(value, schema, "parameter")


@pytest.mark.parametrize(
    ("schema", "hint", "message"),
    [
        ({"minimum": 0, "maximum": 10}, {"factory_steps": True}, "factory_steps"),
        ({"minimum": 0}, {"minimum": -1, "maximum": 1}, "widens the minimum"),
        ({"maximum": 10}, {"minimum": 1, "maximum": 11}, "widens the maximum"),
        ({}, {"minimum": 2, "maximum": 1}, "reversed"),
        ({"type": "integer"}, {"values": []}, "non-empty"),
        ({"type": "integer"}, {"values": [1, 1]}, "duplicates"),
        ({"type": "integer", "minimum": 0}, {"values": [-1]}, "below"),
        ({"type": "integer"}, {}, "requires values"),
    ],
)
def test_parameter_search_hint_validation(schema, hint, message) -> None:
    with pytest.raises(ContractError, match=message):
        contracts._validate_parameter_search_hint("p", schema, hint)


def test_parameter_search_hint_accepts_enum_without_explicit_values() -> None:
    contracts._validate_parameter_search_hint("p", {"enum": ["a", "b"]}, {})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"allow_parameter_adaptation": 1}, "boolean"),
        ({"neighborhood_points": True}, "neighborhood"),
        ({"neighborhood_points": 22}, "neighborhood"),
        ({"allowed_parameter_names": frozenset({"not-qualified"})}, "runtime-qualified"),
        ({"allowed_parameter_names": frozenset({"bad name.x"})}, "portable"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_adaptation_policy_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        AdaptationPolicy(**changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"allowed_mutations": frozenset({"unknown"})}, "unsupported"),
        ({"allowed_mutations": frozenset()}, "at least one"),
        ({"allowed_parameter_names": frozenset({"p"})}, "runtime-qualified"),
        ({"neighborhood_points": 0}, "neighborhood"),
        ({"maximum_runtime_module_changes": -1}, "non-negative"),
        ({"maximum_architectures": 0}, "positive"),
        ({"maximum_complexity_increase": Decimal("-1")}, ">="),
        ({"require_baseline_control": 1}, "boolean"),
        ({"allowed_mutations": frozenset({"entry"})}, "positive module-change"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_improvement_policy_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        ImprovementPolicy(**changes)


def test_time_range_rejects_naive_or_reversed() -> None:
    with pytest.raises(ContractError, match="timezone-aware"):
        TimeRange(datetime(2026, 1, 1), NOW)
    with pytest.raises(ContractError, match="later"):
        TimeRange(NOW, NOW)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"file_format": "json"}, "format"),
        ({"row_count": 0}, "row_count"),
        ({"available_timeframes": ("15m",)}, "base_timeframe"),
        ({"available_timeframes": ("5m", "5m")}, "unique"),
        ({"funding_uri": "funding.csv", "funding_sha256": None}, "requires"),
        ({"funding_sha256": "bad"}, "funding_sha256"),
        ({"exchange_info_sha256": "bad"}, "exchange_info"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_dataset_descriptor_rejects_invalid(descriptor, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        replace(descriptor, **changes)


def _dataset_report(descriptor, **changes) -> DatasetValidationReport:
    values = dict(
        report_id="report-1",
        dataset_id=descriptor.dataset_id,
        dataset_fingerprint=descriptor.fingerprint,
        status=DatasetGateStatus.ACCEPTED,
        checked_at=NOW,
        rows_scanned=descriptor.row_count,
        duplicates=0,
        gaps=0,
        out_of_order=0,
        invalid_ohlc=0,
        invalid_volume=0,
        extreme_outliers=0,
        computed_sha256=descriptor.sha256,
    )
    values.update(changes)
    return DatasetValidationReport(**values)


def test_dataset_validation_report_normalizes_and_rejects() -> None:
    report = _dataset_report(
        type("D", (), {"dataset_id": "d", "fingerprint": SHA, "row_count": 1, "sha256": SHA})(),
        reason_codes=("B", "A", "A"),
        warnings=("w",),
        evidence={"x": [1]},
    )
    assert report.reason_codes == ("A", "B")
    assert isinstance(report.evidence, MappingProxyType)
    with pytest.raises(ContractError, match="reason_codes"):
        replace(report, status=DatasetGateStatus.REJECTED, reason_codes=())
    with pytest.raises(ContractError, match="non-negative"):
        replace(report, gaps=-1)
    with pytest.raises(ContractError, match="schema_version"):
        replace(report, schema_version=2)


def _profile(descriptor, **changes) -> InstrumentProfile:
    values = dict(
        profile_id="profile-1",
        dataset_fingerprint=descriptor.fingerprint,
        symbol=descriptor.symbol,
        market_type=descriptor.market_type,
        generated_at=NOW,
        history_days=Decimal("1"),
        observations=10,
        liquidity_metrics={},
        volatility_metrics={},
        return_distribution={},
        trend_metrics={},
        mean_reversion_metrics={},
        seasonality_metrics={},
        tail_risk_metrics={},
        funding_metrics={},
        cost_sensitivity={},
        stability_metrics={},
    )
    values.update(changes)
    return InstrumentProfile(**values)


def test_instrument_profile_rejects_invalid(descriptor) -> None:
    with pytest.raises(ContractError, match=">="):
        _profile(descriptor, history_days=Decimal("-1"))
    with pytest.raises(ContractError, match="observations"):
        _profile(descriptor, observations=0)
    with pytest.raises(ContractError, match="schema_version"):
        _profile(descriptor, schema_version=2)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"feature_version": "bad"}, "feature_version"),
        ({"point_in_time": False}, "point-in-time"),
        ({"required_timeframes": ("5m", "5m")}, "unique"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_market_regime_rejects_invalid(changes, message) -> None:
    values = dict(
        regime_id="regime-1",
        name="trend",
        description="trend regime",
        feature_version="1.0",
        point_in_time=True,
        required_timeframes=("5m",),
        parameters={},
    )
    values.update(changes)
    with pytest.raises(ContractError, match=message):
        MarketRegimeDefinition(**values)


def _module(**changes) -> ModuleReference:
    values = dict(
        module_id="signal",
        version="1.0.0",
        category=ModuleCategory.SIGNAL,
        import_path="pkg:Signal",
        api_version="1.0",
        state_schema_version="1",
        required_timeframes=("5m",),
        required_features=(),
        produced_features=("setup",),
        required_data=frozenset(),
        supported_markets=frozenset({MarketType.LINEAR_FUTURES}),
        supported_directions=frozenset({Direction.LONG}),
        deterministic=True,
        test_coverage=Decimal("95"),
        maturity=ModuleMaturity.PAPER_READY,
    )
    values.update(changes)
    return ModuleReference(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"version": "bad"}, "version"),
        ({"import_path": ""}, "import_path"),
        ({"supported_markets": frozenset()}, "market"),
        ({"supported_directions": frozenset()}, "direction"),
        ({"deterministic": 1}, "bool"),
        ({"test_coverage": Decimal("101")}, "exceed"),
        ({"required_timeframes": ("5m", "5m")}, "unique"),
        ({"fixed_parameters": {"x": 1}}, "absent"),
        ({"parameter_search_hints": {"x": {"values": [1]}}}, "absent"),
        (
            {
                "parameter_schema": {"x": {}},
                "fixed_parameters": {"x": 1},
                "parameter_search_hints": {"x": {"values": [1]}},
            },
            "also be searchable",
        ),
        ({"parameter_schema": {"x": []}, "fixed_parameters": {"x": 1}}, "must be an object"),
        (
            {"parameter_schema": {"x": {}}, "parameter_search_hints": {"x": []}},
            "hint x must be an object",
        ),
        (
            {"parameter_schema": {"x": {}}, "parameter_search_hints": {"x": {"unknown": 1}}},
            "unsupported fields",
        ),
        ({"evidence_links": ("",)}, "non-empty"),
        ({"source_commit_sha": "bad"}, "source_commit_sha"),
        ({"runtime_component_version": "bad"}, "runtime component version"),
        ({"runtime_role_categories": frozenset({ModuleCategory.ENTRY})}, "contain"),
        (
            {"runtime_role_categories": frozenset({ModuleCategory.SIGNAL, ModuleCategory.ENTRY})},
            "role_facet",
        ),
        ({"role_facet": "bad facet"}, "portable"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_module_reference_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        _module(**changes)


def test_module_reference_validates_fixed_and_search_values() -> None:
    with pytest.raises(ContractError, match="minimum"):
        _module(
            parameter_schema={"x": {"type": "integer", "minimum": 1}}, fixed_parameters={"x": 0}
        )
    with pytest.raises(ContractError, match="duplicates"):
        _module(
            parameter_schema={"x": {"type": "integer"}},
            parameter_search_hints={"x": {"values": [1, 1]}},
        )
    valid = _module(runtime_component_id="runtime", runtime_component_version="1.2.0")
    assert valid.runtime_key == "runtime@1.2.0"


def _hypothesis(**changes) -> StrategyHypothesis:
    values = dict(
        hypothesis_id="hyp-1",
        description="desc",
        expected_edge="edge",
        applicability=("trend",),
        risks=("drawdown",),
        module_candidates=(_module(),),
        parameter_ranges={},
        falsification_criteria={},
        estimated_experiments=1,
        novelty=Decimal("0.5"),
        origin={},
    )
    values.update(changes)
    return StrategyHypothesis(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"description": ""}, "description"),
        ({"expected_edge": ""}, "expected_edge"),
        ({"estimated_experiments": 0}, "estimated_experiments"),
        ({"novelty": Decimal("-1")}, ">="),
        ({"novelty": Decimal("2")}, r"\[0, 1\]"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_strategy_hypothesis_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        _hypothesis(**changes)


@pytest.mark.parametrize(
    ("field_name", "category", "message"),
    [
        ("signal", ModuleCategory.ENTRY, "signal"),
        ("entry", ModuleCategory.SIGNAL, "entry"),
        ("protection", ModuleCategory.SIGNAL, "protection"),
        ("target", ModuleCategory.SIGNAL, "target"),
        ("risk", ModuleCategory.SIGNAL, "risk reference"),
    ],
)
def test_architecture_rejects_wrong_primary_categories(
    architecture, field_name, category, message
) -> None:
    with pytest.raises(ContractError, match=message):
        replace(architecture, **{field_name: module(f"wrong-{field_name}", category)})


def test_architecture_rejects_wrong_collection_categories_and_duplicates(architecture) -> None:
    collection_cases = [
        ("setup_adapters", (module("x-setup", ModuleCategory.SIGNAL),), "setup_adapters"),
        ("risk_adapters", (module("x-risk-adapter", ModuleCategory.SIGNAL),), "risk_adapters"),
        ("context_filters", (module("x-context", ModuleCategory.SIGNAL),), "context_filters"),
        (
            "position_management",
            (module("x-position", ModuleCategory.SIGNAL),),
            "position_management",
        ),
        (
            "portfolio_constraints",
            (module("x-portfolio", ModuleCategory.SIGNAL),),
            "portfolio_constraints",
        ),
    ]
    for field_name, value, message in collection_cases:
        with pytest.raises(ContractError, match=message):
            replace(architecture, **{field_name: value})
    with pytest.raises(ContractError, match="direction"):
        replace(architecture, directions=frozenset())
    with pytest.raises(ContractError, match="duplicate"):
        replace(architecture, context_filters=(module("fvg", ModuleCategory.CONTEXT_FILTER),))
    with pytest.raises(ContractError, match="schema_version"):
        replace(architecture, schema_version=2)


def test_architecture_shared_runtime_contract_and_facets(architecture) -> None:
    roles = frozenset({ModuleCategory.PROTECTION, ModuleCategory.TARGET})
    protection = replace(
        architecture.protection,
        runtime_component_id="shared",
        runtime_component_version="1.0.0",
        runtime_role_categories=roles,
        role_facet="protection",
    )
    target = replace(
        protection,
        module_id="shared-target",
        category=ModuleCategory.TARGET,
        role_facet="target",
    )
    valid = replace(architecture, protection=protection, target=target)
    assert len(valid.runtime_modules) == len(valid.modules) - 1
    with pytest.raises(ContractError, match="undeclared"):
        replace(
            valid,
            target=replace(target, runtime_role_categories=frozenset({ModuleCategory.TARGET})),
        )
    with pytest.raises(ContractError, match="conflicting contracts"):
        replace(valid, target=replace(target, import_path="other:Target"))
    with pytest.raises(ContractError, match="unique role facets"):
        replace(valid, target=replace(target, role_facet="protection"))


def _space(architecture, **changes) -> ParameterSpace:
    values = dict(
        space_id="space-1", architecture_id=architecture.architecture_id, dimensions={"x": (1, 2)}
    )
    values.update(changes)
    return ParameterSpace(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dimensions": {}}, "requires dimensions"),
        ({"dimensions": {"x": []}}, "non-empty tuple"),
        ({"dimensions": {"x": (1, 1)}}, "duplicates"),
        ({"maximum_combinations": 0}, "positive"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_parameter_space_rejects_invalid(architecture, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        _space(architecture, **changes)


def test_resource_budget_rejects_invalid(budget) -> None:
    with pytest.raises(ContractError, match="positive"):
        replace(budget, maximum_experiments=0)
    with pytest.raises(ContractError, match="invalid stage"):
        replace(budget, stage_experiment_limits={"not-stage": 1})
    with pytest.raises(ContractError, match="stage limits exceed"):
        replace(
            budget, maximum_experiments=1, stage_experiment_limits={FactoryStage.COARSE_SEARCH: 2}
        )
    with pytest.raises(ContractError, match="schema_version"):
        replace(budget, schema_version=2)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": "BTCUSDT"}, "symbol"),
        ({"allowed_timeframes": ()}, "cannot be empty"),
        ({"allowed_timeframes": ("1d",)}, "unavailable"),
        ({"initial_capital": Decimal("0")}, "initial_capital"),
        ({"research_risk_fraction": Decimal("2")}, "must not exceed"),
        ({"maximum_drawdown_fraction": Decimal("2")}, "must not exceed"),
        ({"minimum_trade_count": 0}, "minimum_trade_count"),
        ({"random_seed": -1}, "random_seed"),
        ({"allowed_strategy_families": frozenset()}, "cannot be empty"),
        ({"mode": FactoryMode.IMPROVE}, "baseline_strategy_id"),
        ({"adaptation_policy": object()}, "AdaptationPolicy"),
        ({"improvement_policy": object()}, "ImprovementPolicy"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_factory_request_rejects_invalid(factory_request, changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        replace(factory_request, **changes)


def _plan(architecture, descriptor, **changes) -> ExperimentPlan:
    values = dict(
        plan_id="plan-1",
        factory_run_id="run-1",
        stage=FactoryStage.COARSE_SEARCH,
        architecture=architecture,
        parameter_space=_space(architecture),
        dataset_fingerprint=descriptor.fingerprint,
        research_specification={},
        resource_limits={},
        deterministic_seed=1,
        manifest_id="manifest-1",
    )
    values.update(changes)
    return ExperimentPlan(**values)


def test_experiment_plan_and_batch_contracts(architecture, descriptor) -> None:
    plan = _plan(architecture, descriptor)
    with pytest.raises(ContractError, match="different architecture"):
        replace(plan, parameter_space=replace(plan.parameter_space, architecture_id="other"))
    with pytest.raises(ContractError, match="deterministic_seed"):
        replace(plan, deterministic_seed=-1)
    with pytest.raises(ContractError, match="schema_version"):
        replace(plan, schema_version=2)

    batch = ExperimentBatchRequest("batch-1", "run-1", (plan,), "idem-1", NOW)
    with pytest.raises(ContractError, match="contain plans"):
        replace(batch, plans=())
    with pytest.raises(ContractError, match="duplicate"):
        replace(batch, plans=(plan, plan))
    with pytest.raises(ContractError, match="mixes"):
        replace(batch, plans=(replace(plan, plan_id="plan-2", factory_run_id="run-2"),))
    with pytest.raises(ContractError, match="timezone-aware"):
        replace(batch, created_at=datetime(2026, 1, 1))
    with pytest.raises(ContractError, match="schema_version"):
        replace(batch, schema_version=2)

    result = ExperimentBatchResult("batch-1", "run-1", "completed", ("p1",), (), (), (), NOW)
    with pytest.raises(ContractError, match="overlap"):
        replace(result, failed_plan_ids=("p1",))
    with pytest.raises(ContractError, match="batch status"):
        replace(result, status="")
    with pytest.raises(ContractError, match="schema_version"):
        replace(result, schema_version=2)


def _evaluation(candidate_id="candidate-1", **changes) -> CandidateEvaluation:
    values = dict(
        evaluation_id="evaluation-1",
        candidate_id=candidate_id,
        metrics={},
        constraints={},
        pareto_front=0,
        instability_flags=(),
        complexity=Decimal("1"),
        evidence_references=(),
    )
    values.update(changes)
    return CandidateEvaluation(**values)


def _candidate(architecture, **changes) -> CandidateStrategy:
    parameters = changes.pop("parameters", {"x": 1})
    values = dict(
        candidate_id="candidate-1",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=(),
    )
    values.update(changes)
    return CandidateStrategy(**values)


def test_candidate_contracts(architecture) -> None:
    with pytest.raises(ContractError, match="pareto_front"):
        _evaluation(pareto_front=-1)
    with pytest.raises(ContractError, match=">="):
        _evaluation(complexity=Decimal("-1"))
    with pytest.raises(ContractError, match="schema_version"):
        _evaluation(schema_version=2)
    candidate = _candidate(architecture)
    with pytest.raises(ContractError, match="configuration_hash"):
        replace(candidate, configuration_hash="b" * 64)
    with pytest.raises(ContractError, match="different candidate"):
        replace(candidate, evaluation=_evaluation("other"))
    with pytest.raises(ContractError, match="schema_version"):
        replace(candidate, schema_version=2)


def test_robustness_holdout_and_decision_contracts(architecture) -> None:
    candidate = _candidate(architecture)
    robustness = RobustnessReport("robust-1", candidate.candidate_id, True, {}, (), NOW)
    with pytest.raises(ContractError, match="bool"):
        replace(robustness, passed=1)
    with pytest.raises(ContractError, match="failure reasons"):
        replace(robustness, passed=False, failure_reasons=())
    with pytest.raises(ContractError, match="schema_version"):
        replace(robustness, schema_version=2)

    holdout = HoldoutReport(
        "holdout-1",
        candidate.candidate_id,
        TimeRange(NOW, NOW + timedelta(days=1)),
        SHA,
        {},
        True,
        True,
        NOW,
    )
    with pytest.raises(ContractError, match="booleans"):
        replace(holdout, passed=1)
    with pytest.raises(ContractError, match="one-time"):
        replace(holdout, opened_once=False)
    with pytest.raises(ContractError, match="schema_version"):
        replace(holdout, schema_version=2)

    decision = FactoryDecision(
        "decision-1",
        "run-1",
        FactoryStage.COARSE_SEARCH,
        DecisionAction.ACCEPT,
        candidate.candidate_id,
        ("OK",),
        {},
        (),
        {},
        NOW,
    )
    with pytest.raises(ContractError, match="reason codes"):
        replace(decision, reason_codes=())
    with pytest.raises(ContractError, match="schema_version"):
        replace(decision, schema_version=2)


def _audit(candidate_id="candidate-1", **changes) -> StrategyAuditDecision:
    values = dict(
        audit_id="audit-1",
        candidate_id=candidate_id,
        status=AdmissionStatus.RESEARCH_ONLY,
        evidence_package_reference="evidence://1",
        evidence_hash=SHA,
        reason_codes=("RESEARCH",),
        issued_at=NOW,
        auditor_id="auditor-1",
        contract_version="1",
        signature="sig",
    )
    values.update(changes)
    return StrategyAuditDecision.issue(**values)


def test_audit_decision_contract() -> None:
    audit = _audit()
    assert audit.decision_hash == canonical_sha256(audit.hash_payload())
    with pytest.raises(ContractError, match="reason codes"):
        _audit(reason_codes=())
    with pytest.raises(ContractError, match="hash mismatch"):
        replace(audit, decision_hash="c" * 64)
    with pytest.raises(ContractError, match="schema_version"):
        replace(audit, schema_version=2)


def _package(architecture, **changes) -> StrategyPackage:
    robustness = RobustnessReport("robust-1", "candidate-1", True, {}, (), NOW)
    holdout = HoldoutReport(
        "holdout-1",
        "candidate-1",
        TimeRange(NOW, NOW + timedelta(days=1)),
        SHA,
        {},
        True,
        True,
        NOW,
    )
    values = dict(
        strategy_id="strategy-1",
        version="1.0.0",
        symbol="ETHUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        directions=frozenset({Direction.LONG}),
        architecture=architecture,
        parameters={},
        risk_policy={},
        constraints={},
        timeframes=("5m",),
        execution_assumptions={},
        dataset_hashes=(SHA,),
        research_periods={"train": TimeRange(NOW - timedelta(days=2), NOW - timedelta(days=1))},
        commit_sha=GIT,
        random_seeds=(1,),
        key_results={},
        robustness_report=robustness,
        holdout_report=holdout,
        known_weaknesses=(),
        disabled_regimes=(),
        admission_status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_links=(),
        paper_configuration={},
        package_hash=SHA,
    )
    values.update(changes)
    return StrategyPackage(**values)


def test_strategy_package_checkpoint_and_manifest_contracts(architecture, factory_request) -> None:
    package = _package(architecture)
    with pytest.raises(ContractError, match="version"):
        replace(package, version="bad")
    with pytest.raises(ContractError, match="directions"):
        replace(package, directions=frozenset())
    with pytest.raises(ContractError, match="dataset hash"):
        replace(package, dataset_hashes=("bad",))
    with pytest.raises(ContractError, match="never authorize live"):
        replace(package, live_trading_authorized=True)
    with pytest.raises(ContractError, match="robustness and holdout"):
        replace(
            package,
            robustness_report=replace(
                package.robustness_report, passed=False, failure_reasons=("bad",)
            ),
        )
    with pytest.raises(ContractError, match="schema_version"):
        replace(package, schema_version=2)

    checkpoint = StrategyFactoryCheckpoint(
        "run-1",
        factory_request.fingerprint,
        FactoryStatus.CREATED,
        FactoryStage.DATA_GATE,
        0,
        factory_request.dataset.fingerprint,
        GIT,
        "manifest-1",
        {},
        {},
        frozenset(),
        {},
        None,
        NOW,
    )
    with pytest.raises(ContractError, match="stage_sequence"):
        replace(checkpoint, stage_sequence=-1)
    with pytest.raises(ContractError, match="decision_chain_head"):
        replace(checkpoint, decision_chain_head="bad")
    with pytest.raises(ContractError, match="schema_version"):
        replace(checkpoint, schema_version=2)

    manifest = StrategyFactoryManifest(
        "manifest-1",
        "run-1",
        factory_request.fingerprint,
        factory_request.dataset.fingerprint,
        GIT,
        "0.4.2",
        "0.3.0",
        "0.1.0",
        "0.1.0",
        {},
        0,
        NOW,
    )
    with pytest.raises(ContractError, match="random_seed"):
        replace(manifest, random_seed=-1)
    with pytest.raises(ContractError, match="schema_version"):
        replace(manifest, schema_version=2)
