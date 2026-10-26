from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from strategy_factory.canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
    TimeRange,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.package import StrategyPackageBuilder
from strategy_factory.package_intent import (
    create_package_intent,
    request_context,
    verify_intent_mapping,
    verify_package_intent,
)
from strategy_factory.ranking import (
    Constraint,
    Objective,
    ObjectiveDirection,
    ParetoRanker,
)


def audit_decision(candidate_id: str, *, issued_at: datetime) -> StrategyAuditDecision:
    return StrategyAuditDecision.issue(
        audit_id=f"audit-{candidate_id}",
        candidate_id=candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference=f"evidence:{candidate_id}",
        evidence_hash="a" * 64,
        reason_codes=("INDEPENDENT_AUDIT_PASSED",),
        issued_at=issued_at,
        auditor_id="strategy-audit-test",
        contract_version="strategy-audit-port-v1",
        signature="test-signature",
    )


def research_periods(period: TimeRange) -> dict[str, TimeRange]:
    span = period.end - period.start
    quarter = span / 4
    return {
        "train": TimeRange(period.start, period.start + quarter),
        "validation": TimeRange(period.start + quarter, period.start + 2 * quarter),
        "test": TimeRange(period.start + 2 * quarter, period.start + 3 * quarter),
        "holdout": TimeRange(period.start + 3 * quarter, period.end),
    }


def candidate(architecture, candidate_id: str, metrics: dict[str, object], complexity: str):
    evaluation = CandidateEvaluation(
        evaluation_id=f"eval-{candidate_id}",
        candidate_id=candidate_id,
        metrics=metrics,
        constraints={"registry_clean": True},
        pareto_front=0,
        instability_flags=(),
        complexity=Decimal(complexity),
        evidence_references=("evidence-1",),
    )
    parameters = {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": 4,
    }
    return CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("run-1",),
        evaluation=evaluation,
    )


def test_pareto_ranking_and_simplicity_tiebreak(architecture) -> None:
    simple = candidate(
        architecture,
        "candidate-simple",
        {"net_return": "0.20", "max_drawdown": "0.08", "trade_count": 100},
        "2",
    )
    complex_ = candidate(
        architecture,
        "candidate-complex",
        {"net_return": "0.20", "max_drawdown": "0.08", "trade_count": 100},
        "5",
    )
    dominated = candidate(
        architecture,
        "candidate-dominated",
        {"net_return": "0.10", "max_drawdown": "0.12", "trade_count": 100},
        "1",
    )
    ranker = ParetoRanker(
        objectives=(
            Objective("net_return", ObjectiveDirection.MAXIMIZE),
            Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
        ),
        constraints=(Constraint("trade_count", minimum=Decimal("30")),),
    )
    result = ranker.rank((complex_, dominated, simple))
    assert [item.candidate.candidate_id for item in result.ranked[:2]] == [
        "candidate-simple",
        "candidate-complex",
    ]
    assert result.ranked[-1].pareto_front == 2


def test_ranking_rejects_missing_metric(architecture) -> None:
    incomplete = candidate(architecture, "candidate-incomplete", {"net_return": 1}, "1")
    result = ParetoRanker(
        objectives=(
            Objective("net_return", ObjectiveDirection.MAXIMIZE),
            Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
        ),
        constraints=(),
    ).rank((incomplete,))
    assert result.ranked == ()
    assert "MISSING_OBJECTIVE_METRIC" in result.rejected[0].selection_reasons


def test_package_hash_and_paper_only_gate(factory_request, architecture) -> None:
    value = candidate(
        architecture,
        "candidate-package",
        {"net_return": "0.20", "max_drawdown": "0.08"},
        "2",
    )
    now = datetime(2026, 1, 2, tzinfo=UTC)
    robustness = RobustnessReport(
        report_id="robustness-1",
        candidate_id=value.candidate_id,
        passed=True,
        scenario_results={"cost_stress": "pass"},
        failure_reasons=(),
        generated_at=now,
    )
    holdout = HoldoutReport(
        report_id="holdout-1",
        candidate_id=value.candidate_id,
        period=research_periods(factory_request.dataset.period)["holdout"],
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={"profit_factor": "1.25"},
        passed=True,
        opened_once=True,
        generated_at=now,
    )
    builder = StrategyPackageBuilder()
    package = builder.build(
        request=factory_request,
        candidate=value,
        strategy_id="ethusdt-factory",
        version="1.0.0",
        commit_sha="c" * 40,
        research_periods=research_periods(factory_request.dataset.period),
        robustness=robustness,
        holdout=holdout,
        key_results={"net_return": "0.20"},
        known_weaknesses=("limited crisis coverage",),
        disabled_regimes=("illiquid",),
        evidence_links=("evidence-package-1",),
        paper_configuration={"symbol": "ETHUSDT"},
        risk_policy={"risk_fraction": "0.005"},
        execution_assumptions={"maker_fee": "0.0002"},
        audit_decision=audit_decision(value.candidate_id, issued_at=now.replace(day=3)),
    )
    builder.verify(package)
    assert package.admission_status.value == "APPROVED_FOR_PAPER"
    assert package.paper_configuration["execution_mode"] == "paper"
    assert not package.live_trading_authorized


def test_package_without_independent_audit_is_insufficient_evidence(
    factory_request, architecture
) -> None:
    value = candidate(architecture, "candidate-no-audit", {"net_return": "0.10"}, "1")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    periods = research_periods(factory_request.dataset.period)
    package = StrategyPackageBuilder().build(
        request=factory_request,
        candidate=value,
        strategy_id="ethusdt-no-audit",
        version="1.0.0",
        commit_sha="b" * 40,
        research_periods=periods,
        robustness=RobustnessReport("robustness-no-audit", value.candidate_id, True, {}, (), now),
        holdout=HoldoutReport(
            "holdout-no-audit",
            value.candidate_id,
            periods["holdout"],
            factory_request.dataset.fingerprint,
            {"profit_factor": "1.1"},
            True,
            True,
            now,
        ),
        key_results={},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("evidence",),
        paper_configuration={},
        risk_policy={},
        execution_assumptions={},
    )
    assert package.admission_status is AdmissionStatus.INSUFFICIENT_EVIDENCE


def test_v2_package_intent_binds_pre_audit_package_without_audit_hash_cycle(
    factory_request, architecture
) -> None:
    value = candidate(architecture, "candidate-intent", {"net_return": "0.20"}, "2")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    periods = research_periods(factory_request.dataset.period)
    robustness = RobustnessReport("robustness-intent", value.candidate_id, True, {}, (), now)
    holdout = HoldoutReport(
        "holdout-intent",
        value.candidate_id,
        periods["holdout"],
        factory_request.dataset.fingerprint,
        {"profit_factor": "1.25", "trade_count": 40},
        True,
        True,
        now,
    )
    builder = StrategyPackageBuilder()
    common = {
        "request": factory_request,
        "candidate": value,
        "strategy_id": "ethusdt-intent",
        "version": "1.0.0",
        "commit_sha": "d" * 40,
        "research_periods": periods,
        "robustness": robustness,
        "holdout": holdout,
        "known_weaknesses": ("limited crisis coverage",),
        "disabled_regimes": ("illiquid",),
        "paper_configuration": {"factory_run_id": "run-intent"},
        "risk_policy": {
            "research_risk_fraction": factory_request.research_risk_fraction,
            "initial_capital": factory_request.initial_capital,
        },
        "execution_assumptions": {
            "exchange": factory_request.exchange,
            "market_type": factory_request.market_type.value,
        },
    }
    draft = builder.build(
        **common,
        key_results={
            "candidate_metrics": value.evaluation.metrics,
            "holdout_metrics": holdout.metrics,
        },
        evidence_links=("candidate-evidence", "robustness-intent", "holdout-intent"),
    )
    reserved_link_draft = replace(
        draft,
        evidence_links=(*draft.evidence_links, "final_audit-untrusted-preexisting-link"),
    )
    with pytest.raises(CompatibilityError, match="reserved generated audit evidence-link prefix"):
        create_package_intent(reserved_link_draft, factory_request)
    unsupported_request = replace(
        factory_request,
        objectives={**dict(factory_request.objectives), "fee_model": "maker"},
    )
    with pytest.raises(CompatibilityError, match="unsupported decision-relevant Factory objective"):
        request_context(unsupported_request)
    unsupported_execution_request = replace(
        factory_request,
        objectives={"maker_fee": "not-a-schema-field"},
    )
    with pytest.raises(CompatibilityError, match="unsupported decision-relevant Factory objective"):
        request_context(unsupported_execution_request)
    cost_stress_request = replace(
        factory_request,
        objectives={
            **dict(factory_request.objectives),
            "cost_scenarios": (
                {
                    "scenario_id": "cost-double-v1",
                    "roles": ("oos",),
                    "execution_cost_multiplier": 2,
                },
            ),
        },
    )
    assert request_context(cost_stress_request)["objectives"]["cost_scenarios"] == [
        {
            "scenario_id": "cost-double-v1",
            "roles": ["oos"],
            "execution_cost_multiplier": 2,
        }
    ]
    unsupported_robustness_request = replace(
        factory_request,
        objectives={
            **dict(factory_request.objectives),
            "robustness_scenarios": ({"name": "delay"},),
        },
    )
    with pytest.raises(CompatibilityError, match="no supported typed owner contract"):
        request_context(unsupported_robustness_request)
    intent = create_package_intent(draft, factory_request).as_mapping()
    assert "package_hash" not in intent["package_projection"]
    assert "audit_id" not in intent["package_projection"]
    assert intent["package_projection"]["key_results"] == draft.key_results

    decision = StrategyAuditDecision.issue(
        audit_id="audit-intent-v2",
        candidate_id=value.candidate_id,
        status=AdmissionStatus.RESEARCH_ONLY,
        evidence_package_reference="strategy-factory-artifact:strategy_audit_envelope-intent",
        evidence_hash="e" * 64,
        reason_codes=("RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE",),
        issued_at=now.replace(day=3),
        auditor_id="strategy-audit-test",
        contract_version="strategy-audit-port-v2",
        signature="test-signature-v2",
    )
    package = builder.build(
        **common,
        key_results={
            "candidate_metrics": value.evaluation.metrics,
            "holdout_metrics": holdout.metrics,
            "audit_status": decision.status.value,
        },
        evidence_links=(
            "candidate-evidence",
            "robustness-intent",
            "holdout-intent",
            decision.audit_id,
            decision.decision_hash,
            decision.evidence_package_reference,
            "strategy_audit_envelope-intent",
        ),
        audit_decision=decision,
        package_intent=intent,
    )
    verify_package_intent(intent, package)
    assert package.package_hash != draft.package_hash
    assert package.constraints["package_intent_hash"] == intent["intent_hash"]

    changed = replace(
        package,
        key_results={
            **dict(package.key_results),
            "holdout_metrics": {"profit_factor": "9.99", "trade_count": 40},
        },
        package_hash="0" * 64,
    )
    changed = replace(changed, package_hash=builder.compute_hash(changed))
    with pytest.raises(CompatibilityError, match="differs from signed package intent"):
        verify_package_intent(intent, changed)


@pytest.mark.parametrize(
    ("objective", "message"),
    [
        ({"strategy_version": " "}, "strategy_version"),
        ({"known_weaknesses": ["ok", ""]}, "known_weaknesses"),
        ({"disabled_regimes": "illiquid"}, "disabled_regimes"),
        ({"walk_forward": {"mode": "random"}}, "mode is unsupported"),
        ({"walk_forward": {"train_seconds": True}}, "invalid integer value"),
        ({"walk_forward": {"unrecognized": 4}}, "fields differ"),
        ({"risk_policy": {"maximum_risk": 0.1}}, "unsupported decision-relevant field"),
        ({"execution_assumptions": {"exchange": "other"}}, "conflicts with typed Factory request"),
        ({"cost_scenarios": [{"scenario_id": "fee-stress-v1"}]}, "fields differ"),
        (
            {
                "cost_scenarios": [
                    {
                        "scenario_id": "cost-double-v1",
                        "roles": ["train"],
                        "execution_cost_multiplier": 2,
                    }
                ]
            },
            "roles must be the supported OOS role",
        ),
        (
            {
                "cost_scenarios": [
                    {
                        "scenario_id": "unknown-cost-v1",
                        "roles": ["oos"],
                        "execution_cost_multiplier": 2,
                    }
                ]
            },
            "scenario_id is unsupported",
        ),
        (
            {
                "cost_scenarios": [
                    {
                        "scenario_id": "cost-double-v1",
                        "roles": ["oos"],
                        "execution_cost_multiplier": 3,
                    }
                ]
            },
            "execution_cost_multiplier is unsupported",
        ),
        ({"cost_scenarios": "cost-double-v1"}, "cost_scenarios must be a sequence"),
        ({"cost_scenarios": ["cost-double-v1"]}, "must be a mapping"),
        ({"risk_policy": []}, "must be a mapping"),
        ({"robustness_scenarios": [{"name": "delay"}]}, "no supported typed owner contract"),
        ({"api_key": "hidden"}, "secret-like field"),
        ({"invalid metric": "maximize"}, "unsupported decision-relevant Factory objective"),
        ({"1metric": "maximize"}, "unsupported decision-relevant Factory objective"),
        ({"valid_metric": "neutral"}, "unsupported decision-relevant Factory objective"),
    ],
)
def test_v2_request_projection_rejects_unsupported_or_malformed_objectives(
    factory_request, objective, message
) -> None:
    request = replace(factory_request, objectives=objective)
    with pytest.raises(CompatibilityError, match=message):
        request_context(request)


@pytest.mark.parametrize(
    "objectives",
    [
        {"net_return": "maximize"},
        {
            "walk_forward": {
                "mode": "rolling",
                "train_seconds": 100,
                "validation_seconds": 10,
                "oos_seconds": 20,
                "step_seconds": 5,
                "purge_seconds": 0,
                "embargo_seconds": 0,
                "minimum_history_seconds": 0,
            }
        },
        {
            "cost_scenarios": [
                {
                    "scenario_id": "cost-double-v1",
                    "roles": ("oos",),
                    "execution_cost_multiplier": 2,
                }
            ],
            "robustness_scenarios": [],
        },
    ],
)
def test_v2_request_projection_preserves_supported_typed_objectives(factory_request, objectives):
    request = replace(factory_request, objectives=objectives)
    projected = request_context(request)
    assert projected["objectives"] == strict_json_loads(canonical_json_bytes(objectives))


def test_v2_request_projection_preserves_matching_risk_and_execution_context(factory_request):
    objectives = {
        "risk_policy": {
            "research_risk_fraction": factory_request.research_risk_fraction,
            "initial_capital": factory_request.initial_capital,
        },
        "execution_assumptions": {
            "exchange": factory_request.exchange,
            "market_type": factory_request.market_type.value,
        },
    }
    projected = request_context(replace(factory_request, objectives=objectives))
    assert projected["objectives"] == strict_json_loads(canonical_json_bytes(objectives))


@pytest.mark.parametrize(
    ("field", "uri"),
    [
        ("uri", "https://user:credential@example.test/data.csv"),
        ("uri", "https://example.test/data.csv?PASSWORD=credential"),
        ("uri", "https://example.test/data.csv?%61pi%5Fkey=credential"),
        ("funding_uri", "https://user:credential@example.test/funding.csv"),
        ("funding_uri", "https://example.test/funding.csv?PASSWORD=credential"),
        ("funding_uri", "https://example.test/funding.csv?%61pi%5Fkey=credential"),
    ],
)
def test_v2_request_projection_rejects_credential_bearing_dataset_uris(factory_request, field, uri):
    changes = {field: uri}
    if field == "funding_uri":
        changes["funding_sha256"] = "f" * 64
    dataset = replace(factory_request.dataset, **changes)
    with pytest.raises(CompatibilityError, match="dataset URI"):
        request_context(replace(factory_request, dataset=dataset))


def test_v2_request_projection_keeps_ordinary_dataset_uri_query(factory_request):
    dataset = replace(
        factory_request.dataset,
        uri="https://example.test/data.csv?version=2&format=parquet",
    )
    projected = request_context(replace(factory_request, dataset=dataset))
    assert projected["dataset"]["uri"] == dataset.uri

    funding_dataset = replace(
        factory_request.dataset,
        funding_uri="synthetic://fixture/funding.csv?version=2",
        funding_sha256="f" * 64,
    )
    projected_funding = request_context(replace(factory_request, dataset=funding_dataset))
    assert projected_funding["dataset"]["funding_uri"] == funding_dataset.funding_uri
    assert projected_funding["dataset"]["funding_sha256"] == funding_dataset.funding_sha256


def test_v2_intent_mapping_checks_digest_and_schema_types(factory_request) -> None:
    context = dict(request_context(factory_request))
    projection = {"schema_version": 1}
    fingerprint = canonical_sha256(context)
    payload = {
        "schema_version": 1,
        "request_fingerprint": fingerprint,
        "request_context": context,
        "package_projection": projection,
    }
    intent = {**payload, "intent_hash": canonical_sha256(payload)}
    verify_intent_mapping(intent)

    malformed = dict(intent)
    malformed["schema_version"] = True
    with pytest.raises(CompatibilityError, match="unsupported package intent schema"):
        verify_intent_mapping(malformed)

    malformed = dict(intent)
    malformed["intent_hash"] = "0" * 64
    with pytest.raises(CompatibilityError, match="intent hash mismatch"):
        verify_intent_mapping(malformed)

    malformed = dict(intent)
    malformed_projection = dict(projection)
    malformed_projection["schema_version"] = True
    malformed_body = {**payload, "package_projection": malformed_projection}
    malformed = {**malformed_body, "intent_hash": canonical_sha256(malformed_body)}
    with pytest.raises(CompatibilityError, match="projection schema is malformed"):
        verify_intent_mapping(malformed)

    malformed = dict(intent)
    malformed["unexpected"] = "ignored?"
    with pytest.raises(CompatibilityError, match="fields differ"):
        verify_intent_mapping(malformed)


def test_package_tampering_detected(factory_request, architecture) -> None:
    from dataclasses import replace

    value = candidate(architecture, "candidate-tamper", {"net_return": 1}, "1")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    robustness = RobustnessReport("robustness-2", value.candidate_id, True, {}, (), now)
    holdout = HoldoutReport(
        "holdout-2",
        value.candidate_id,
        research_periods(factory_request.dataset.period)["holdout"],
        factory_request.dataset.fingerprint,
        {},
        True,
        True,
        now,
    )
    builder = StrategyPackageBuilder()
    package = builder.build(
        request=factory_request,
        candidate=value,
        strategy_id="ethusdt-tamper",
        version="1.0.0",
        commit_sha="d" * 40,
        research_periods=research_periods(factory_request.dataset.period),
        robustness=robustness,
        holdout=holdout,
        key_results={},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=(),
        paper_configuration={},
        risk_policy={},
        execution_assumptions={},
    )
    changed = replace(package, parameters={"changed": True})
    with pytest.raises(CompatibilityError, match="hash mismatch"):
        builder.verify(changed)


def test_package_rejects_out_of_range_parameter(factory_request, architecture) -> None:
    value = candidate(architecture, "candidate-bad-param", {"net_return": 1}, "1")
    bad_parameters = dict(value.parameters)
    bad_parameters["fixed_r.reward_r"] = 99
    value = replace(
        value,
        parameters=bad_parameters,
        configuration_hash=canonical_sha256(bad_parameters),
    )
    now = datetime(2026, 1, 2, tzinfo=UTC)
    robustness = RobustnessReport("robustness-bad", value.candidate_id, True, {}, (), now)
    periods = research_periods(factory_request.dataset.period)
    holdout = HoldoutReport(
        "holdout-bad",
        value.candidate_id,
        periods["holdout"],
        factory_request.dataset.fingerprint,
        {},
        True,
        True,
        now,
    )
    with pytest.raises(CompatibilityError, match="above maximum"):
        StrategyPackageBuilder().build(
            request=factory_request,
            candidate=value,
            strategy_id="bad-param",
            version="1.0.0",
            commit_sha="e" * 40,
            research_periods=periods,
            robustness=robustness,
            holdout=holdout,
            key_results={},
            known_weaknesses=(),
            disabled_regimes=(),
            evidence_links=("evidence",),
            paper_configuration={},
            risk_policy={},
            execution_assumptions={},
        )


def test_package_rejects_secret_material(factory_request, architecture) -> None:
    value = candidate(architecture, "candidate-secret", {"net_return": 1}, "1")
    now = datetime(2026, 1, 2, tzinfo=UTC)
    periods = research_periods(factory_request.dataset.period)
    robustness = RobustnessReport("robustness-secret", value.candidate_id, True, {}, (), now)
    holdout = HoldoutReport(
        "holdout-secret",
        value.candidate_id,
        periods["holdout"],
        factory_request.dataset.fingerprint,
        {},
        True,
        True,
        now,
    )
    with pytest.raises(CompatibilityError, match="secret-like"):
        StrategyPackageBuilder().build(
            request=factory_request,
            candidate=value,
            strategy_id="secret",
            version="1.0.0",
            commit_sha="f" * 40,
            research_periods=periods,
            robustness=robustness,
            holdout=holdout,
            key_results={},
            known_weaknesses=(),
            disabled_regimes=(),
            evidence_links=("evidence",),
            paper_configuration={"api_key": "forbidden"},
            risk_policy={},
            execution_assumptions={},
        )
