from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_ranking_package import candidate, research_periods

from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateStrategy,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
    TimeRange,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.package import (
    StrategyPackageBuilder,
    _numeric,
    _reject_secret_material,
    _validate_parameter_value,
    _validate_research_periods,
)

_NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _audit(
    candidate_id: str,
    *,
    status: AdmissionStatus = AdmissionStatus.APPROVED_FOR_PAPER,
    issued_at: datetime = _NOW + timedelta(hours=2),
) -> StrategyAuditDecision:
    return StrategyAuditDecision.issue(
        audit_id=f"audit-{candidate_id}-{status.value.lower()}",
        candidate_id=candidate_id,
        status=status,
        evidence_package_reference=f"evidence:{candidate_id}",
        evidence_hash="a" * 64,
        reason_codes=(f"AUDIT_{status.value}",),
        issued_at=issued_at,
        auditor_id="strategy-audit-edge",
        contract_version="strategy-audit-port-v1",
        signature="edge-signature",
    )


def _bundle(factory_request, architecture, *, candidate_id: str = "candidate-edge"):
    value = candidate(
        architecture,
        candidate_id,
        {"net_return": "0.20", "max_drawdown": "0.08"},
        "2",
    )
    periods = research_periods(factory_request.dataset.period)
    robustness = RobustnessReport(
        report_id=f"robustness-{candidate_id}",
        candidate_id=candidate_id,
        passed=True,
        scenario_results={"cost_stress": "pass"},
        failure_reasons=(),
        generated_at=_NOW,
    )
    holdout = HoldoutReport(
        report_id=f"holdout-{candidate_id}",
        candidate_id=candidate_id,
        period=periods["holdout"],
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={"profit_factor": "1.25"},
        passed=True,
        opened_once=True,
        generated_at=_NOW + timedelta(hours=1),
    )
    kwargs = {
        "request": factory_request,
        "candidate": value,
        "strategy_id": "edge-strategy",
        "version": "1.0.0",
        "commit_sha": "c" * 40,
        "research_periods": periods,
        "robustness": robustness,
        "holdout": holdout,
        "key_results": {"net_return": "0.20"},
        "known_weaknesses": ("edge weakness",),
        "disabled_regimes": ("illiquid",),
        "evidence_links": ("evidence-package",),
        "paper_configuration": {"symbol": factory_request.symbol},
        "risk_policy": {"risk_fraction": "0.005"},
        "execution_assumptions": {"maker_fee": "0.0002"},
        "audit_decision": _audit(candidate_id),
    }
    return value, periods, robustness, holdout, kwargs


def _candidate_with_parameters(value: CandidateStrategy, parameters: dict[str, object]):
    return replace(
        value,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
    )


def test_build_rejects_missing_or_cross_candidate_evidence(factory_request, architecture) -> None:
    value, _, robustness, holdout, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()

    with pytest.raises(CompatibilityError, match="candidate has no evaluation"):
        builder.build(**{**kwargs, "candidate": replace(value, evaluation=None)})
    with pytest.raises(CompatibilityError, match="robustness report belongs"):
        builder.build(
            **{
                **kwargs,
                "robustness": replace(robustness, candidate_id="another-candidate"),
            }
        )
    with pytest.raises(CompatibilityError, match="holdout report belongs"):
        builder.build(
            **{
                **kwargs,
                "holdout": replace(holdout, candidate_id="another-candidate"),
            }
        )


def test_build_rejects_holdout_lineage_and_timing_drift(factory_request, architecture) -> None:
    _, periods, robustness, holdout, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()

    with pytest.raises(CompatibilityError, match="different dataset descriptor"):
        builder.build(
            **{
                **kwargs,
                "holdout": replace(holdout, dataset_fingerprint="b" * 64),
            }
        )
    without_holdout = dict(periods)
    without_holdout.pop("holdout")
    with pytest.raises(CompatibilityError, match="bind the holdout report"):
        builder.build(**{**kwargs, "research_periods": without_holdout})
    shifted = TimeRange(
        periods["holdout"].start - timedelta(minutes=1),
        periods["holdout"].end,
    )
    with pytest.raises(CompatibilityError, match="bind the holdout report"):
        builder.build(**{**kwargs, "research_periods": {**periods, "holdout": shifted}})
    request = replace(factory_request, explicit_holdout=periods["test"])
    with pytest.raises(CompatibilityError, match="explicit holdout policy"):
        builder.build(**{**kwargs, "request": request})
    late_robustness = replace(
        robustness,
        generated_at=holdout.generated_at + timedelta(seconds=1),
    )
    with pytest.raises(CompatibilityError, match="must precede holdout"):
        builder.build(**{**kwargs, "robustness": late_robustness})


def test_build_rejects_audit_lineage_or_pre_holdout_decision(factory_request, architecture) -> None:
    value, _, _, holdout, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()

    with pytest.raises(CompatibilityError, match="audit decision belongs"):
        builder.build(
            **{
                **kwargs,
                "audit_decision": _audit("another-candidate"),
            }
        )
    with pytest.raises(CompatibilityError, match="must follow holdout"):
        builder.build(
            **{
                **kwargs,
                "audit_decision": _audit(
                    value.candidate_id,
                    issued_at=holdout.generated_at - timedelta(seconds=1),
                ),
            }
        )


@pytest.mark.parametrize(
    ("audit_status", "expected"),
    [
        (AdmissionStatus.REJECTED, AdmissionStatus.REJECTED),
        (
            AdmissionStatus.INSUFFICIENT_EVIDENCE,
            AdmissionStatus.INSUFFICIENT_EVIDENCE,
        ),
        (AdmissionStatus.RESEARCH_ONLY, AdmissionStatus.RESEARCH_ONLY),
    ],
)
def test_build_preserves_fail_closed_audit_admission(
    factory_request,
    architecture,
    audit_status: AdmissionStatus,
    expected: AdmissionStatus,
) -> None:
    value, _, _, _, kwargs = _bundle(factory_request, architecture)
    package = StrategyPackageBuilder().build(
        **{
            **kwargs,
            "audit_decision": _audit(value.candidate_id, status=audit_status),
        }
    )
    assert package.admission_status is expected


def test_approved_audit_without_prerequisite_gates_is_research_only(
    factory_request, architecture
) -> None:
    value, _, robustness, _, kwargs = _bundle(factory_request, architecture)
    failed = replace(
        robustness,
        passed=False,
        failure_reasons=("stress failed",),
    )
    package = StrategyPackageBuilder().build(**{**kwargs, "robustness": failed})
    assert package.admission_status is AdmissionStatus.RESEARCH_ONLY

    evaluation = replace(
        value.evaluation,
        constraints={"registry_clean": False},
        instability_flags=("unstable",),
    )
    degraded = replace(value, evaluation=evaluation)
    package = StrategyPackageBuilder().build(
        **{**kwargs, "candidate": degraded, "evidence_links": ()}
    )
    assert package.admission_status is AdmissionStatus.RESEARCH_ONLY


@pytest.mark.parametrize(
    ("metadata_name", "metadata_value"),
    [
        ("architecture_id", "different-architecture"),
        ("architecture_fingerprint", "f" * 64),
        ("modules", ["wrong@1.0.0"]),
        ("parameters", {"wrong": True}),
    ],
)
def test_resolved_parameters_rejects_architecture_metadata_conflict(
    factory_request,
    architecture,
    metadata_name: str,
    metadata_value: object,
) -> None:
    value, _, _, _, kwargs = _bundle(factory_request, architecture)
    parameters = dict(value.parameters)
    parameters[metadata_name] = metadata_value
    changed = _candidate_with_parameters(value, parameters)
    with pytest.raises(CompatibilityError, match="metadata conflicts"):
        StrategyPackageBuilder().build(**{**kwargs, "candidate": changed})


@pytest.mark.parametrize("identifier_value", ["", "   ", 7])
def test_resolved_parameters_rejects_invalid_factory_metadata(
    factory_request, architecture, identifier_value: object
) -> None:
    value, _, _, _, kwargs = _bundle(factory_request, architecture)
    parameters = dict(value.parameters)
    parameters["factory_run_id"] = identifier_value
    changed = _candidate_with_parameters(value, parameters)
    with pytest.raises(CompatibilityError, match="metadata is invalid"):
        StrategyPackageBuilder().build(**{**kwargs, "candidate": changed})


def test_resolved_parameters_rejects_unqualified_unknown_and_schema_missing(
    factory_request, architecture
) -> None:
    value, _, _, _, kwargs = _bundle(factory_request, architecture)
    cases = [
        ("unqualified", "not runtime-qualified"),
        ("unknown_component.value", "unknown runtime component"),
        ("fixed_r.unknown", "absent from runtime schema"),
    ]
    for name, message in cases:
        parameters = dict(value.parameters)
        parameters[name] = 1
        changed = _candidate_with_parameters(value, parameters)
        with pytest.raises(CompatibilityError, match=message):
            StrategyPackageBuilder().build(**{**kwargs, "candidate": changed})


def test_resolved_parameters_rejects_fixed_parameter_conflict(
    factory_request, architecture
) -> None:
    fixed_risk = replace(
        architecture.risk,
        fixed_parameters={"risk_fraction": "0.005"},
    )
    fixed_architecture = replace(architecture, risk=fixed_risk)
    value, _, _, _, kwargs = _bundle(factory_request, fixed_architecture)
    parameters = dict(value.parameters)
    parameters["fixed_fractional.risk_fraction"] = "0.006"
    changed = _candidate_with_parameters(value, parameters)
    with pytest.raises(CompatibilityError, match="conflicts with fixed"):
        StrategyPackageBuilder().build(**{**kwargs, "candidate": changed})


def test_candidate_parameters_round_trip_and_module_shape_failures(
    factory_request, architecture
) -> None:
    _, _, _, _, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()
    package = builder.build(**kwargs)
    assert builder.candidate_parameters(package) == {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": 4,
    }

    missing = dict(package.parameters)
    missing.pop(next(iter(missing)))
    with pytest.raises(CompatibilityError, match="missing="):
        builder.candidate_parameters(replace(package, parameters=missing))
    extra = {**dict(package.parameters), "extra@1.0.0": {}}
    with pytest.raises(CompatibilityError, match="extra="):
        builder.candidate_parameters(replace(package, parameters=extra))

    runtime_key = next(iter(package.parameters))
    invalid_shape = dict(package.parameters)
    invalid_shape[runtime_key] = "not-a-mapping"
    with pytest.raises(CompatibilityError, match="must be a mapping"):
        builder.candidate_parameters(replace(package, parameters=invalid_shape))


def test_candidate_parameters_rejects_unknown_or_changed_fixed_values(
    factory_request, architecture
) -> None:
    fixed_risk = replace(
        architecture.risk,
        fixed_parameters={"risk_fraction": "0.005"},
    )
    fixed_architecture = replace(architecture, risk=fixed_risk)
    _, _, _, _, kwargs = _bundle(factory_request, fixed_architecture)
    builder = StrategyPackageBuilder()
    package = builder.build(**kwargs)
    risk_key = fixed_risk.runtime_key

    unknown = {key: dict(values) for key, values in package.parameters.items()}
    unknown[risk_key]["unexpected"] = 1
    with pytest.raises(CompatibilityError, match="unknown runtime parameters"):
        builder.candidate_parameters(replace(package, parameters=unknown))

    changed = {key: dict(values) for key, values in package.parameters.items()}
    changed[risk_key]["risk_fraction"] = "0.006"
    with pytest.raises(CompatibilityError, match="changed fixed parameter"):
        builder.candidate_parameters(replace(package, parameters=changed))

    missing = {key: dict(values) for key, values in package.parameters.items()}
    missing[risk_key].pop("risk_fraction")
    with pytest.raises(CompatibilityError, match="changed fixed parameter"):
        builder.candidate_parameters(replace(package, parameters=missing))


def test_candidate_parameters_rejects_ambiguous_runtime_component_ids(
    factory_request, architecture
) -> None:
    target = replace(
        architecture.target,
        runtime_component_id=architecture.risk.runtime_component_id,
        runtime_component_version="2.0.0",
    )
    ambiguous_architecture = replace(architecture, target=target)
    _, _, _, _, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()
    package = builder.build(**kwargs)
    ambiguous_package = replace(package, architecture=ambiguous_architecture)
    with pytest.raises(CompatibilityError, match="component IDs are ambiguous"):
        builder.candidate_parameters(ambiguous_package)


def test_verify_rejects_live_mode_and_hash_tampering(factory_request, architecture) -> None:
    _, _, _, _, kwargs = _bundle(factory_request, architecture)
    builder = StrategyPackageBuilder()
    package = builder.build(**kwargs)

    live = copy.copy(package)
    object.__setattr__(live, "live_trading_authorized", True)
    with pytest.raises(CompatibilityError, match="live authorization"):
        builder.verify(live)

    non_paper = replace(
        package,
        paper_configuration={"execution_mode": "live"},
        package_hash=package.package_hash,
    )
    with pytest.raises(CompatibilityError, match="must be paper"):
        builder.verify(non_paper)


def test_research_period_validation_rejects_missing_and_overlap(factory_request) -> None:
    periods = research_periods(factory_request.dataset.period)
    incomplete = dict(periods)
    incomplete.pop("validation")
    with pytest.raises(CompatibilityError, match="incomplete: validation"):
        _validate_research_periods(incomplete)

    overlap = dict(periods)
    overlap["validation"] = TimeRange(
        periods["train"].end - timedelta(minutes=1),
        periods["validation"].end,
    )
    with pytest.raises(CompatibilityError, match="overlap or are out of order"):
        _validate_research_periods(overlap)


@pytest.mark.parametrize(
    ("schema", "value", "message"),
    [
        ("not-a-schema", 1, "schema is invalid"),
        ({"enum": ["a", "b"]}, "c", "outside enum"),
        ({"type": "boolean"}, 1, "must be boolean"),
        ({"type": "integer"}, True, "must be integer"),
        ({"type": "integer"}, "1", "must be integer"),
        ({"type": "number"}, "bad", "must be numeric"),
        ({"type": "decimal"}, float("inf"), "must be finite"),
        ({"type": "string"}, 1, "must be string"),
        ({"type": "array"}, "not-array", "must be an array"),
        ({"minimum": 2}, 1, "below minimum"),
        ({"maximum": 2}, 3, "above maximum"),
    ],
)
def test_parameter_schema_validation_rejects_invalid_values(
    schema: object,
    value: object,
    message: str,
) -> None:
    with pytest.raises(CompatibilityError, match=message):
        _validate_parameter_value(schema, value, "component.parameter")


@pytest.mark.parametrize("value", [True, None, object(), "NaN", "Infinity"])
def test_numeric_rejects_non_numeric_or_non_finite(value: object) -> None:
    with pytest.raises(CompatibilityError, match="must be (numeric|finite)"):
        _numeric(value, "value")


def test_secret_scanner_finds_nested_mapping_and_sequence() -> None:
    with pytest.raises(CompatibilityError, match=r"package\.outer\[0\]"):
        _reject_secret_material({"outer": [{"refresh-token": "forbidden"}]})
    _reject_secret_material({"outer": [{"public_identifier": "allowed"}]})
