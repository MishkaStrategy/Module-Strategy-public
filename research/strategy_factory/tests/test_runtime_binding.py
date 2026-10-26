from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from modular_core import ModuleDecision, ModuleManifest, StrategyModule
from strategy_audit_validation import (
    AuditDecision,
    AuditOutcome,
    AuditUnavailableError,
    EvidenceTamperedError,
)
from test_ranking_package import candidate, research_periods

import strategy_factory.runtime_binding as runtime_binding
from strategy_factory.audit_envelope import EVIDENCE_REFERENCE_PREFIX
from strategy_factory.canonical import canonical_sha256
from strategy_factory.catalog_v06 import ModuleRoleSpec, TrustedModuleSpec
from strategy_factory.contracts import (
    AdmissionStatus,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.package import StrategyPackageBuilder
from strategy_factory.runtime_binding import StrategyPackageRuntimeBinder

_NOW = datetime(2026, 4, 1, tzinfo=UTC)


class AuditLookup:
    def __init__(self, decision: AuditDecision | Exception) -> None:
        self.decision = decision
        self.calls: list[tuple[str, str]] = []
        self.write_calls = 0

    def load_verified_stored_decision(self, *, audit_id: str, decision_hash: str) -> AuditDecision:
        self.calls.append((audit_id, decision_hash))
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision

    def audit(self, **_kwargs: object) -> None:
        self.write_calls += 1
        raise AssertionError("runtime binding must never create an audit decision")


def _audit_envelope(
    request,
    value,
    periods,
    robustness,
    *,
    run_id: str,
    manifest_id: str,
    commit_sha: str,
    risk_fraction: str,
    drawdown_fraction: str,
    execution_exchange: str,
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "created_at": _NOW,
        "candidate": {
            "candidate_id": value.candidate_id,
            "configuration_hash": value.configuration_hash,
            "parameters": dict(value.parameters),
            "live_trading_authorized": False,
        },
        "dataset_lineage": {
            "dataset_id": request.dataset.dataset_id,
            "registry_version": "synthetic-1",
            "instrument": request.symbol,
            "exchange": request.exchange,
            "market_type": request.market_type.value,
            "manifest_sha256": "1" * 64,
            "dataset_sha256": request.dataset.sha256,
            "timeframes": request.allowed_timeframes,
            "immutable": True,
            "tamper_verified": True,
        },
        "research": {
            "candidate_id": value.candidate_id,
            "configuration_hash": value.configuration_hash,
            "periods": {
                name: {"start": item.start, "end": item.end} for name, item in periods.items()
            },
            "holdout_isolated": True,
            "holdout_opened_once": True,
            "random_seeds": (request.random_seed,),
            "results_evidence": {"registry_package_hashes": ("2" * 64,)},
        },
        "robustness": {
            "candidate_id": value.candidate_id,
            "report_sha256": canonical_sha256(robustness),
            "passed": robustness.passed,
        },
        "execution_assumptions": {
            "mode": "paper",
            "exchange": execution_exchange,
            "market_type": request.market_type.value,
            "live_trading_authorized": False,
            "private_exchange_access": False,
            "order_placement": False,
        },
        "risk_policy": {
            "paper_only": True,
            "maximum_risk_fraction": risk_fraction,
            "maximum_drawdown_fraction": drawdown_fraction,
        },
    }
    body["package_id"] = "pending"
    package_body = dict(body)
    package_body.pop("package_id")
    seed = {
        "factory_run_id": run_id,
        "factory_manifest_id": manifest_id,
        "factory_commit_sha": commit_sha,
        **package_body,
    }
    body["package_id"] = f"sf-audit-{canonical_sha256(seed)[:24]}"
    return body


def _bound_package(factory_request, architecture, *, include_architecture_metadata=True):
    value = candidate(
        architecture,
        "candidate-runtime-binding",
        {"net_return": "0.20", "max_drawdown": "0.08"},
        "2",
    )
    if include_architecture_metadata:
        value_parameters = {
            **value.parameters,
            "architecture_id": architecture.architecture_id,
            "architecture_fingerprint": architecture.fingerprint,
            "modules": tuple(item.key for item in architecture.modules),
            "parameters": dict(architecture.parameters),
        }
        value = replace(
            value,
            parameters=value_parameters,
            configuration_hash=canonical_sha256(value_parameters),
        )
    periods = research_periods(factory_request.dataset.period)
    robustness = RobustnessReport(
        report_id="robustness-runtime-binding",
        candidate_id=value.candidate_id,
        passed=True,
        scenario_results={"synthetic": "pass"},
        failure_reasons=(),
        generated_at=_NOW,
    )
    holdout = HoldoutReport(
        report_id="holdout-runtime-binding",
        candidate_id=value.candidate_id,
        period=periods["holdout"],
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={"trade_count": 100},
        passed=True,
        opened_once=True,
        generated_at=_NOW + timedelta(hours=1),
    )
    run_id = "factory-run-runtime-binding"
    manifest_id = "factory-manifest-runtime-binding"
    commit_sha = "c" * 40
    envelope = _audit_envelope(
        factory_request,
        value,
        periods,
        robustness,
        run_id=run_id,
        manifest_id=manifest_id,
        commit_sha=commit_sha,
        risk_fraction=format(factory_request.research_risk_fraction, "f"),
        drawdown_fraction=format(factory_request.maximum_drawdown_fraction, "f"),
        execution_exchange=factory_request.exchange,
    )
    evidence_reference = (
        EVIDENCE_REFERENCE_PREFIX + "strategy_audit_envelope-" + canonical_sha256(envelope)
    )
    evidence_hash = canonical_sha256(envelope)
    factory_decision = StrategyAuditDecision.issue(
        audit_id="audit-runtime-binding",
        candidate_id=value.candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference=evidence_reference,
        evidence_hash=evidence_hash,
        reason_codes=("SYNTHETIC_APPROVAL",),
        issued_at=_NOW + timedelta(hours=2),
        auditor_id="synthetic-auditor",
        contract_version="strategy-audit-port-v1",
        signature="hmac-sha256:synthetic-signature",
    )
    package = StrategyPackageBuilder().build(
        request=factory_request,
        candidate=value,
        strategy_id="runtime-binding-strategy",
        version="1.0.0",
        commit_sha=commit_sha,
        research_periods=periods,
        robustness=robustness,
        holdout=holdout,
        key_results={"synthetic": True},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("synthetic-evidence",),
        paper_configuration={
            "factory_run_id": run_id,
            "factory_manifest_id": manifest_id,
            "candidate_id": value.candidate_id,
            "symbol": factory_request.symbol,
            "market_type": factory_request.market_type.value,
        },
        risk_policy={
            "research_risk_fraction": factory_request.research_risk_fraction,
            "initial_capital": factory_request.initial_capital,
        },
        execution_assumptions={
            "exchange": factory_request.exchange,
            "market_type": factory_request.market_type.value,
        },
        audit_decision=factory_decision,
    )
    decision = AuditDecision(
        audit_id=factory_decision.audit_id,
        candidate_id=factory_decision.candidate_id,
        outcome=AuditOutcome.APPROVED_FOR_PAPER,
        evidence_package_reference=evidence_reference,
        evidence_hash=evidence_hash,
        reason_codes=factory_decision.reason_codes,
        issued_at=factory_decision.issued_at,
        auditor_id=factory_decision.auditor_id,
        contract_version=factory_decision.contract_version,
        signature=factory_decision.signature,
        decision_hash=factory_decision.decision_hash,
    )
    return package, decision, envelope


def _reidentify_envelope(envelope, package):
    signed = dict(envelope)
    signed.pop("package_id", None)
    seed = {
        "factory_run_id": package.paper_configuration["factory_run_id"],
        "factory_manifest_id": package.paper_configuration["factory_manifest_id"],
        "factory_commit_sha": package.commit_sha,
        **signed,
    }
    signed["package_id"] = f"sf-audit-{canonical_sha256(seed)[:24]}"
    return signed


def _synthetic_specs(architecture):
    grouped = {}
    for item in architecture.modules:
        grouped.setdefault(item.runtime_key, []).append(item)
    specs = []
    for runtime_key, refs in grouped.items():
        item = refs[0]
        specs.append(
            TrustedModuleSpec(
                spec_id=f"synthetic-{runtime_key}",
                import_path=item.import_path,
                expected_manifest_name=str(item.runtime_component_id),
                expected_manifest_version=str(item.runtime_component_version),
                roles=tuple(
                    ModuleRoleSpec(ref.module_id, ref.category, ref.role_facet) for ref in refs
                ),
                # Constructor defaults are not the fixed/search contract. The
                # candidate's audited search value may legitimately differ.
                constructor_parameters=(
                    {"reward_r": 1} if item.runtime_component_id == "fixed_r" else {}
                ),
                fixed_parameter_names=frozenset(item.fixed_parameters),
                parameter_search_hints=item.parameter_search_hints,
                required_data=item.required_data,
                supported_markets=item.supported_markets,
                supported_directions=item.supported_directions,
                deterministic=item.deterministic,
                compatibility_tags=item.compatibility_tags,
                conflict_tags=item.conflict_tags,
            )
        )
    return tuple(specs)


def _fake_factory(spec, parameters, architecture):
    ref = next(item for item in architecture.modules if item.module_id == spec.roles[0].module_id)

    class SyntheticModule(StrategyModule):
        name = spec.expected_manifest_name

        def __init__(self):
            self.parameters = dict(parameters)
            manifest_schema = {}
            for name, rules in ref.parameter_schema.items():
                manifest_rules = {
                    rule: (
                        float(value)
                        if rule in {"minimum", "maximum"} and isinstance(value, str)
                        else value
                    )
                    for rule, value in rules.items()
                }
                if {"minimum", "maximum"} & set(rules) and "type" not in rules:
                    manifest_rules["type"] = "number"
                manifest_schema[name] = manifest_rules
            self.manifest = ModuleManifest(
                name=spec.expected_manifest_name,
                version=spec.expected_manifest_version,
                api_version=ref.api_version,
                state_schema_version=ref.state_schema_version,
                parameters_schema=manifest_schema,
            )
            # The broad Strategy Factory fixture predates ModuleManifest's
            # namespaced-feature validation. Preserve its exact metadata for
            # this boundary test after constructing a real manifest instance.
            object.__setattr__(self.manifest, "required_features", ref.required_features)
            object.__setattr__(self.manifest, "optional_features", ref.optional_features)
            object.__setattr__(self.manifest, "produced_features", ref.produced_features)

        def on_bar(self, _view):
            return ModuleDecision()

    return SyntheticModule()


def _binder(monkeypatch, architecture, decision, envelope, *, lookup=None):
    monkeypatch.setattr(
        runtime_binding, "v06_trusted_specs", lambda: _synthetic_specs(architecture)
    )
    monkeypatch.setattr(
        runtime_binding,
        "_instantiate_trusted_spec",
        lambda spec, parameters: _fake_factory(spec, parameters, architecture),
    )
    audit_lookup = lookup or AuditLookup(decision)
    return (
        StrategyPackageRuntimeBinder(
            audit_lookup=audit_lookup,
            evidence_lookup=lambda _reference: envelope,
        ),
        audit_lookup,
    )


def test_binds_exact_synthetic_module_graph_but_never_qualifies_runner(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    binder, audit_lookup = _binder(monkeypatch, architecture, decision, envelope)

    binding = binder.bind(package, expected_package_hash=package.package_hash)

    assert binding.runtime_keys == tuple(
        item.runtime_key for item in package.architecture.runtime_modules
    )
    assert len(binding.modules) == len(binding.runtime_keys)
    assert binding.qualification_complete is False
    assert "authenticated audit evidence does not bind every package request/constraint field" in (
        binding.qualification_blockers
    )
    assert len(binding.qualification_blockers) >= 3
    with pytest.raises(CompatibilityError, match="only partially bound"):
        binding.require_runtime_qualified()
    assert audit_lookup.calls == [(decision.audit_id, decision.decision_hash)]
    assert audit_lookup.write_calls == 0


def test_requires_trusted_expected_package_hash_before_audit_lookup(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    binder, lookup = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match="differs from trusted configuration"):
        binder.bind(package, expected_package_hash="e" * 64)
    assert lookup.calls == []


@pytest.mark.parametrize("field", ["runtime_component_version", "import_path"])
def test_rejects_unknown_runtime_recipe_and_never_executes_package_import_path(
    monkeypatch, factory_request, architecture, field
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    ref = package.architecture.signal
    changed = replace(ref, **{field: "attacker:payload" if field == "import_path" else "9.9.9"})
    changed_architecture = replace(package.architecture, signal=changed)
    parameters = dict(package.parameters)
    if field == "runtime_component_version":
        parameters["fvg@9.9.9"] = parameters.pop("fvg@1.0.0")
    package = replace(
        package,
        architecture=changed_architecture,
        parameters=parameters,
        package_hash="0" * 64,
    )
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    imported: list[str] = []
    monkeypatch.setattr(
        runtime_binding.importlib, "import_module", lambda name: imported.append(name)
    )
    changed_group = tuple(
        item for item in changed_architecture.modules if item.runtime_key == changed.runtime_key
    )
    with pytest.raises(CompatibilityError, match="trusted recipe"):
        runtime_binding._instantiate_runtime_modules(
            changed_group,
            (changed,),
            package.parameters,
            _synthetic_specs(architecture),
        )
    assert imported == []


def test_rejects_package_parameter_drift_against_original_audited_candidate(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    changed_parameters = {key: dict(value) for key, value in package.parameters.items()}
    changed_parameters["fixed_r@1.0.0"]["reward_r"] = 5
    package = replace(package, parameters=changed_parameters, package_hash="0" * 64)
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    binder, _ = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match="parameters differ from audited candidate"):
        binder.bind(package, expected_package_hash=package.package_hash)


def test_rejects_signed_candidate_without_architecture_metadata_before_constructors(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(
        factory_request, architecture, include_architecture_metadata=False
    )
    imported: list[str] = []
    monkeypatch.setattr(
        runtime_binding.importlib, "import_module", lambda name: imported.append(name)
    )
    binder, _ = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match="does not bind package architecture metadata"):
        binder.bind(package, expected_package_hash=package.package_hash)
    assert imported == []


def test_rejects_rehashed_architecture_drift_against_signed_candidate_metadata(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    changed_architecture = replace(package.architecture, architecture_id="attacker-architecture")
    package = replace(package, architecture=changed_architecture, package_hash="0" * 64)
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    binder, _ = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match="does not bind package architecture metadata"):
        binder.bind(package, expected_package_hash=package.package_hash)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_rejects_missing_or_extra_runtime_parameter_components(
    monkeypatch, factory_request, architecture, mutation
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    parameters = {key: dict(value) for key, value in package.parameters.items()}
    if mutation == "missing":
        parameters.pop("fvg@1.0.0")
    else:
        parameters["unregistered@1.0.0"] = {}
    package = replace(package, parameters=parameters, package_hash="0" * 64)
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    binder, _ = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match="parameters differ from audited candidate"):
        binder.bind(package, expected_package_hash=package.package_hash)


def test_rejects_manifest_drift_after_trusted_constructor_resolution(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    binder, _ = _binder(monkeypatch, architecture, decision, envelope)

    def wrong_manifest(spec, parameters):
        instance = _fake_factory(spec, parameters, architecture)
        expected = instance.manifest
        bad_manifest = ModuleManifest(
            name="unexpected-module",
            version=expected.version,
            api_version=expected.api_version,
            state_schema_version=expected.state_schema_version,
            parameters_schema=expected.parameters_schema,
        )
        object.__setattr__(bad_manifest, "required_features", expected.required_features)
        object.__setattr__(bad_manifest, "optional_features", expected.optional_features)
        object.__setattr__(bad_manifest, "produced_features", expected.produced_features)
        instance.manifest = bad_manifest
        return instance

    monkeypatch.setattr(runtime_binding, "_instantiate_trusted_spec", wrong_manifest)
    with pytest.raises(CompatibilityError, match="manifest identity differs"):
        binder.bind(package, expected_package_hash=package.package_hash)


@pytest.mark.parametrize("outcome", [AuditOutcome.REJECTED, AuditOutcome.UNAVAILABLE])
def test_refuses_non_approved_or_unavailable_package_audit(
    monkeypatch, factory_request, architecture, outcome
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    changed = SimpleNamespace(
        audit_id=decision.audit_id,
        candidate_id=decision.candidate_id,
        outcome=outcome,
        evidence_package_reference=decision.evidence_package_reference,
        evidence_hash=decision.evidence_hash,
        contract_version=decision.contract_version,
        decision_hash=decision.decision_hash,
        live_trading_authorized=False,
    )
    binder, _ = _binder(monkeypatch, architecture, changed, envelope)

    with pytest.raises(CompatibilityError):
        binder.bind(package, expected_package_hash=package.package_hash)


def test_rejects_package_policy_mismatch_and_legacy_unbound_package(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    changed_envelope = dict(envelope)
    changed_envelope["risk_policy"] = {
        **dict(envelope["risk_policy"]),
        "maximum_drawdown_fraction": "0.99",
    }
    package_body = dict(changed_envelope)
    package_body.pop("package_id")
    changed_seed = {
        "factory_run_id": package.paper_configuration["factory_run_id"],
        "factory_manifest_id": package.paper_configuration["factory_manifest_id"],
        "factory_commit_sha": package.commit_sha,
        **package_body,
    }
    changed_envelope["package_id"] = f"sf-audit-{canonical_sha256(changed_seed)[:24]}"
    changed_evidence_hash = canonical_sha256(changed_envelope)
    constraints = dict(package.constraints)
    constraints["audit_evidence_hash"] = changed_evidence_hash
    package = replace(package, constraints=constraints, package_hash="0" * 64)
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    changed_decision = SimpleNamespace(
        audit_id=decision.audit_id,
        candidate_id=decision.candidate_id,
        outcome=AuditOutcome.APPROVED_FOR_PAPER,
        evidence_package_reference=decision.evidence_package_reference,
        evidence_hash=changed_evidence_hash,
        contract_version=decision.contract_version,
        decision_hash=decision.decision_hash,
        live_trading_authorized=False,
    )
    binder, _ = _binder(monkeypatch, architecture, changed_decision, changed_envelope)
    with pytest.raises(CompatibilityError, match="risk policy differs"):
        binder.bind(package, expected_package_hash=package.package_hash)

    constraints = dict(package.constraints)
    constraints.pop("audit_evidence_package_reference")
    legacy = replace(package, constraints=constraints, package_hash="0" * 64)
    legacy = replace(legacy, package_hash=StrategyPackageBuilder().compute_hash(legacy))
    binder, lookup = _binder(monkeypatch, architecture, decision, envelope)
    with pytest.raises(CompatibilityError, match="audit_evidence_package_reference"):
        binder.bind(legacy, expected_package_hash=legacy.package_hash)
    assert lookup.calls == []


@pytest.mark.parametrize(
    ("family", "signal_id", "setup_id"),
    [
        ("breakout_return", "breakout_return", "setup_adapter.breakout_return_v1"),
        ("liquidity_sweep", "liquidity_sweep", "setup_adapter.liquidity_sweep_v1"),
        ("fvg_rt", "fvg_rt", "setup_adapter.fvg_rt_composite_v1"),
    ],
)
def test_binds_real_v06_profiles_to_factory_envelope_and_stored_hmac_decision(
    tmp_path, factory_request, family, signal_id, setup_id
) -> None:
    """Exercise real catalog constructors without reading any market-data source."""
    from strategy_audit_validation import (
        AuditOutcome,
    )
    from test_independent_auditor_e2e import (
        AUDIT_NOW,
        MANIFEST_HASH,
        RESULT_HASH,
        DatasetSource,
        ResultsSource,
        _auditor,
        _periods,
    )

    from strategy_factory.audit_envelope import (
        DatasetAuditLineage,
        FactoryAuditEnvelopeBuilder,
        ResultsAuditEvidence,
    )
    from strategy_factory.catalog_v06 import TrustedRepositoryCatalogBuilder, v06_trusted_specs
    from strategy_factory.contracts import (
        CandidateEvaluation,
        CandidateStrategy,
        Direction,
        ModuleCategory,
        StrategyFactoryManifest,
    )
    from strategy_factory.grammar import GrammarPolicy, StrategyGrammar
    from strategy_factory.runtime_binding import _instantiate_trusted_spec, _spec_matches_group

    catalog_specs = tuple(
        spec
        for spec in v06_trusted_specs()
        if spec.spec_id
        in {
            "breakout_return",
            "liquidity_sweep",
            "fvg_rt",
            "setup_adapter_breakout_return_v1",
            "setup_adapter_liquidity_sweep_v1",
            "setup_adapter_fvg_rt_composite_v1",
            "entry_model",
            "protection_target",
            "risk_request_composer",
            "risk_sizing",
        }
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha="a" * 40).build(catalog_specs)
    assert report.complete
    descriptor = replace(
        factory_request.dataset,
        available_timeframes=("5m", "15m", "1h", "4h", "1d"),
    )
    request = replace(factory_request, dataset=descriptor)
    family_tag = {
        "breakout_return": "strategy_family:breakout",
        "liquidity_sweep": "strategy_family:liquidity_sweep",
        "fvg_rt": "strategy_family:fvg_rt",
    }[family]
    generation = StrategyGrammar(
        report.catalog,
        GrammarPolicy(
            maximum_context_filters=0,
            maximum_setup_adapters=1,
            maximum_risk_adapters=1,
            maximum_position_management=0,
            maximum_portfolio_constraints=0,
        ),
    ).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        allowed_signal_module_ids=frozenset(
            ref.module_id
            for ref in report.catalog.all()
            if ref.category is ModuleCategory.SIGNAL and family_tag in ref.compatibility_tags
        ),
        external_features=frozenset({"instrument.rules", "portfolio.risk_snapshot"}),
        runtime_data=frozenset({"instrument_rules", "portfolio_snapshot"}),
        maximum_architectures=100,
    )
    architecture = next(
        candidate_architecture
        for candidate_architecture in generation.architectures
        if {item.module_id for item in candidate_architecture.modules} >= {signal_id, setup_id}
    )
    assert architecture.signal.module_id == signal_id
    setup_ref = next(item for item in architecture.setup_adapters if item.module_id == setup_id)
    setup_spec = next(
        spec
        for spec in catalog_specs
        if spec.expected_manifest_name == "setup_adapter" and spec.roles[0].module_id == setup_id
    )
    assert dict(setup_ref.fixed_parameters) == dict(setup_spec.constructor_parameters)
    altered_profile_ref = replace(
        setup_ref,
        fixed_parameters={"sources": ({"feature_key": "attacker.feature"},)},
    )
    assert not _spec_matches_group(setup_spec, setup_ref.runtime_key, (altered_profile_ref,))
    malformed_profile_ref = replace(setup_ref, fixed_parameters={})
    assert not _spec_matches_group(setup_spec, setup_ref.runtime_key, (malformed_profile_ref,))
    signal_ref = architecture.signal
    signal_spec = next(
        spec
        for spec in catalog_specs
        if len(spec.roles) == 1 and _spec_matches_group(spec, signal_ref.runtime_key, (signal_ref,))
    )
    overclaimed_signal_roles = replace(
        signal_ref,
        runtime_role_categories=frozenset(
            {*signal_ref.runtime_role_categories, ModuleCategory.PORTFOLIO_CONSTRAINT}
        ),
        role_facet=signal_ref.role_facet or "overclaimed-runtime-role",
    )
    assert not _spec_matches_group(signal_spec, signal_ref.runtime_key, (overclaimed_signal_roles,))

    resolved: dict[str, Any] = {}
    specs = v06_trusted_specs()
    grouped: dict[str, list[Any]] = {}
    for ref in architecture.modules:
        grouped.setdefault(ref.runtime_key, []).append(ref)
    for runtime_key, refs in grouped.items():
        matches = [spec for spec in specs if _spec_matches_group(spec, runtime_key, refs)]
        assert len(matches) == 1
        instance = _instantiate_trusted_spec(matches[0], matches[0].constructor_parameters)
        for name, value in instance.parameters.items():
            resolved[f"{instance.manifest.name}.{name}"] = value

    candidate_id = f"candidate-real-{family}"
    candidate_parameters = {
        **resolved,
        "architecture_id": architecture.architecture_id,
        "architecture_fingerprint": architecture.fingerprint,
        "modules": tuple(item.key for item in architecture.modules),
        "parameters": dict(architecture.parameters),
    }
    evaluation = CandidateEvaluation(
        evaluation_id=f"evaluation-real-{family}",
        candidate_id=candidate_id,
        metrics={"parameter_stability": Decimal("0.90")},
        constraints={"synthetic_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal("2"),
        evidence_references=(f"results-{family}",),
    )
    candidate = CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=candidate_parameters,
        configuration_hash=canonical_sha256(candidate_parameters),
        lineage_references=(f"run-{family}",),
        evaluation=evaluation,
    )
    periods = _periods(descriptor.period.start)
    robustness = RobustnessReport(
        report_id=f"robustness-{family}",
        candidate_id=candidate_id,
        passed=True,
        scenario_results={"synthetic": "pass"},
        failure_reasons=(),
        generated_at=AUDIT_NOW - timedelta(hours=3),
    )
    holdout = HoldoutReport(
        report_id=f"holdout-{family}",
        candidate_id=candidate_id,
        period=periods.holdout,
        dataset_fingerprint=descriptor.fingerprint,
        metrics={
            "profit_factor": Decimal("1.4"),
            "max_drawdown": Decimal("0.1"),
            "trade_count": 120,
        },
        passed=True,
        opened_once=True,
        generated_at=AUDIT_NOW - timedelta(hours=2),
    )
    run_id = f"run-{family}"
    manifest_id = f"manifest-{family}"
    commit_sha = "a" * 40
    manifest = StrategyFactoryManifest(
        manifest_id=manifest_id,
        run_id=run_id,
        request_fingerprint=request.fingerprint,
        dataset_fingerprint=descriptor.fingerprint,
        commit_sha=commit_sha,
        core_version="0.4.2",
        research_runner_version="synthetic",
        results_registry_version="synthetic",
        strategy_factory_version="0.1.0",
        module_versions={},
        random_seed=request.random_seed,
        created_at=AUDIT_NOW - timedelta(hours=4),
    )
    lineage = DatasetAuditLineage(
        dataset_id=descriptor.dataset_id,
        registry_version="synthetic-registry-v1",
        instrument=request.symbol,
        exchange=request.exchange,
        market_type=request.market_type.value,
        manifest_sha256=MANIFEST_HASH,
        dataset_sha256=descriptor.sha256,
        timeframes=descriptor.available_timeframes,
        immutable=True,
        tamper_verified=True,
        funding_lineage_sha256="3" * 64,
    )
    results = ResultsAuditEvidence(
        candidate_id=candidate_id,
        configuration_hash=candidate.configuration_hash,
        experiment_ids=(f"experiment-{family}",),
        registry_package_hashes=(RESULT_HASH,),
    )
    envelope = FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(results),
    ).build(
        request=request,
        manifest=manifest,
        candidate=candidate,
        robustness=robustness,
        holdout=holdout,
        periods=periods,
        experiment_id=f"experiment-{family}",
    )
    evidence_reference = (
        EVIDENCE_REFERENCE_PREFIX + "strategy_audit_envelope-" + canonical_sha256(envelope)
    )
    auditor, store = _auditor(tmp_path / f"{family}.sqlite3")
    decision = auditor.audit(evidence_package_reference=evidence_reference, evidence=envelope)
    assert decision.outcome is AuditOutcome.APPROVED_FOR_PAPER
    factory_decision = StrategyAuditDecision.issue(
        audit_id=decision.audit_id,
        candidate_id=decision.candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference=decision.evidence_package_reference,
        evidence_hash=decision.evidence_hash,
        reason_codes=decision.reason_codes,
        issued_at=decision.issued_at,
        auditor_id=decision.auditor_id,
        contract_version=decision.contract_version,
        signature=decision.signature,
    )
    package = StrategyPackageBuilder().build(
        request=request,
        candidate=candidate,
        strategy_id=f"strategy-{family}",
        version="1.0.0",
        commit_sha=commit_sha,
        research_periods={
            "train": periods.train,
            "validation": periods.validation,
            "test": periods.test,
            "holdout": periods.holdout,
        },
        robustness=robustness,
        holdout=holdout,
        key_results={"synthetic": True},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=(f"synthetic-{family}",),
        paper_configuration={
            "factory_run_id": run_id,
            "factory_manifest_id": manifest_id,
            "candidate_id": candidate_id,
            "symbol": request.symbol,
            "market_type": request.market_type.value,
        },
        risk_policy={
            "research_risk_fraction": request.research_risk_fraction,
            "initial_capital": request.initial_capital,
        },
        execution_assumptions={
            "exchange": request.exchange,
            "market_type": request.market_type.value,
        },
        audit_decision=factory_decision,
    )
    changes_before = store._connection.total_changes
    try:
        binding = StrategyPackageRuntimeBinder(
            audit_lookup=auditor,
            evidence_lookup=lambda reference: (
                envelope
                if reference == evidence_reference
                else (_ for _ in ()).throw(KeyError(reference))
            ),
        ).bind(package, expected_package_hash=package.package_hash)
        assert store._connection.total_changes == changes_before
        assert binding.qualification_complete is False
        assert len(binding.modules) == len(architecture.runtime_modules)
        assert all(isinstance(module, StrategyModule) for module in binding.modules)
        assert any(module.manifest.name == "setup_adapter" for module in binding.modules)
        with pytest.raises(CompatibilityError, match="only partially bound"):
            binding.require_runtime_qualified()
    finally:
        store.close()


def test_rejects_tampered_signed_envelope_and_request_candidate_mismatch(
    monkeypatch, factory_request, architecture
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    tampered_envelope = dict(envelope)
    tampered_envelope["candidate"] = {
        **dict(envelope["candidate"]),
        "candidate_id": "different-candidate",
    }
    binder, lookup = _binder(monkeypatch, architecture, decision, tampered_envelope)
    with pytest.raises(CompatibilityError, match="stored audit envelope hash differs"):
        binder.bind(package, expected_package_hash=package.package_hash)
    assert lookup.write_calls == 0

    constraints = dict(package.constraints)
    constraints["candidate_id"] = "different-candidate"
    mismatched = replace(package, constraints=constraints, package_hash="0" * 64)
    mismatched = replace(mismatched, package_hash=StrategyPackageBuilder().compute_hash(mismatched))
    binder, lookup = _binder(monkeypatch, architecture, decision, envelope)
    with pytest.raises(CompatibilityError, match="candidate identity is not consistently bound"):
        binder.bind(mismatched, expected_package_hash=mismatched.package_hash)
    assert lookup.write_calls == 0


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("architecture", "architecture metadata"),
        ("periods", "research periods"),
        ("seeds", "random seeds"),
        ("risk", "risk policy differs"),
        ("execution", "execution assumptions differ"),
        ("robustness", "robustness evidence differs"),
        ("auxiliary_dataset", "dataset lineage differs"),
        ("empty_datasets", "dataset lineage differs"),
        ("missing_envelope_field", "missing package lineage fields"),
        ("candidate_lineage", "audit candidate lineage differs"),
        ("candidate_parameters_missing", "audit candidate parameters are unavailable"),
        ("package_identity", "evidence package identity differs"),
        ("risk_invalid", "package risk policy is not audit-bindable"),
        ("risk_nonfinite", "audit risk policy differs"),
        ("package_execution", "package execution assumptions differ"),
    ],
)
def test_rejects_signed_envelope_lineage_drift(
    factory_request, architecture, mutation, expected_error
) -> None:
    package, _decision, envelope = _bound_package(factory_request, architecture)
    original_package = package
    changed = dict(envelope)
    if mutation == "architecture":
        candidate_value = dict(changed["candidate"])
        raw_parameters = dict(candidate_value["parameters"])
        raw_parameters["architecture_fingerprint"] = "different-fingerprint"
        candidate_value["parameters"] = raw_parameters
        changed["candidate"] = candidate_value
    elif mutation == "periods":
        research = dict(changed["research"])
        research["periods"] = {"train": {"start": "wrong", "end": "wrong"}}
        changed["research"] = research
    elif mutation == "seeds":
        research = dict(changed["research"])
        research["random_seeds"] = (999,)
        changed["research"] = research
    elif mutation == "risk":
        changed["risk_policy"] = {
            **dict(changed["risk_policy"]),
            "maximum_risk_fraction": "0.99",
        }
    elif mutation == "execution":
        changed["execution_assumptions"] = {
            **dict(changed["execution_assumptions"]),
            "exchange": "UNBOUND-EXCHANGE",
        }
    elif mutation == "robustness":
        changed["robustness"] = {
            **dict(changed["robustness"]),
            "report_sha256": "f" * 64,
        }
    elif mutation == "auxiliary_dataset":
        auxiliary_hash = "9" * 64
        package = replace(package, dataset_hashes=(*package.dataset_hashes[:1], auxiliary_hash))
        changed["dataset_lineage"] = {
            **dict(changed["dataset_lineage"]),
            "dataset_sha256": auxiliary_hash,
        }
    elif mutation == "empty_datasets":
        package = replace(package, dataset_hashes=())
    elif mutation == "missing_envelope_field":
        changed.pop("robustness")
    elif mutation == "candidate_lineage":
        changed["candidate"] = {
            **dict(changed["candidate"]),
            "configuration_hash": "f" * 64,
        }
    elif mutation == "candidate_parameters_missing":
        changed["candidate"] = {**dict(changed["candidate"]), "parameters": None}
    elif mutation == "package_identity":
        paper = dict(package.paper_configuration)
        paper["factory_run_id"] = "different-run"
        package = replace(package, paper_configuration=paper)
    elif mutation == "risk_invalid":
        changed["risk_policy"] = {
            **dict(changed["risk_policy"]),
            "maximum_risk_fraction": "invalid",
        }
    elif mutation == "risk_nonfinite":
        changed["risk_policy"] = {
            **dict(changed["risk_policy"]),
            "maximum_risk_fraction": "NaN",
        }
    elif mutation == "package_execution":
        package = replace(
            package,
            execution_assumptions={**dict(package.execution_assumptions), "exchange": "OTHER"},
        )

    signed = _reidentify_envelope(
        changed, original_package if mutation == "package_identity" else package
    )
    decision = SimpleNamespace(evidence_hash=canonical_sha256(signed))
    with pytest.raises(CompatibilityError, match=expected_error):
        runtime_binding._verify_audit_envelope(package, signed, decision)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audit_id", "different-audit"),
        ("decision_hash", "0" * 64),
        ("outcome", AuditOutcome.REJECTED),
        ("live_trading_authorized", True),
        ("candidate_id", "different-candidate"),
        ("evidence_package_reference", "different-reference"),
        ("evidence_hash", "0" * 64),
        ("contract_version", "different-contract"),
    ],
)
def test_rejects_each_stored_decision_lineage_mismatch_without_writes(
    monkeypatch, factory_request, architecture, field, value
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    mismatched_decision = SimpleNamespace(
        audit_id=decision.audit_id,
        decision_hash=decision.decision_hash,
        outcome=decision.outcome,
        live_trading_authorized=decision.live_trading_authorized,
        candidate_id=decision.candidate_id,
        evidence_package_reference=decision.evidence_package_reference,
        evidence_hash=decision.evidence_hash,
        contract_version=decision.contract_version,
    )
    setattr(mismatched_decision, field, value)
    binder, lookup = _binder(monkeypatch, architecture, mismatched_decision, envelope)

    with pytest.raises(CompatibilityError, match="stored audit decision does not match"):
        binder.bind(package, expected_package_hash=package.package_hash)
    assert lookup.write_calls == 0


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("unapproved", "not approved for PAPER"),
        ("audit_status", "audit constraint is not PAPER-approved"),
        ("contract_missing", "original audit contract binding"),
        ("candidate_hash_missing", "candidate configuration binding"),
        ("evidence_hash_invalid", "audit_evidence_hash must be"),
    ],
)
def test_package_admission_and_original_audit_constraints_fail_before_lookup(
    monkeypatch, factory_request, architecture, mutation, expected_error
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    if mutation == "unapproved":
        package = replace(package, admission_status=AdmissionStatus.REJECTED)
    else:
        constraints = dict(package.constraints)
        if mutation == "audit_status":
            constraints["audit_status"] = AdmissionStatus.REJECTED.value
        elif mutation == "contract_missing":
            constraints.pop("audit_contract_version")
        elif mutation == "candidate_hash_missing":
            constraints.pop("candidate_configuration_hash")
        elif mutation == "evidence_hash_invalid":
            constraints["audit_evidence_hash"] = "invalid"
        package = replace(package, constraints=constraints)
    package = replace(package, package_hash="0" * 64)
    package = replace(package, package_hash=StrategyPackageBuilder().compute_hash(package))
    binder, lookup = _binder(monkeypatch, architecture, decision, envelope)

    with pytest.raises(CompatibilityError, match=expected_error):
        binder.bind(package, expected_package_hash=package.package_hash)
    assert lookup.calls == []
    assert lookup.write_calls == 0


def test_rejects_invalid_runtime_parameter_map_and_inconsistent_module_closure(
    monkeypatch, factory_request, architecture
) -> None:
    package, _decision, _envelope = _bound_package(factory_request, architecture)
    runtime_modules = package.architecture.runtime_modules
    specs = _synthetic_specs(architecture)
    first_key = runtime_modules[0].runtime_key
    missing_parameters = dict(package.parameters)
    missing_parameters.pop(first_key)
    constructed: list[str] = []
    monkeypatch.setattr(
        runtime_binding,
        "_instantiate_trusted_spec",
        lambda spec, _parameters: constructed.append(spec.spec_id),
    )
    with pytest.raises(CompatibilityError, match="runtime parameters are unavailable"):
        runtime_binding._instantiate_runtime_modules(
            package.architecture.modules, runtime_modules, missing_parameters, specs
        )
    assert constructed == []

    grouped_refs: dict[str, list[Any]] = {}
    for module_ref in architecture.modules:
        grouped_refs.setdefault(module_ref.runtime_key, []).append(module_ref)
    runtime_key, refs = next(
        (key, refs) for key, refs in grouped_refs.items() if key.split("@", 1)[0] != "setup_adapter"
    )
    spec = next(
        item for item in specs if runtime_binding._spec_matches_group(item, runtime_key, refs)
    )
    fixed_refs = tuple(
        replace(
            item,
            fixed_parameters={"synthetic_fixed": "trusted"},
            parameter_schema={
                **dict(item.parameter_schema),
                "synthetic_fixed": {"type": "string"},
            },
        )
        for item in refs
    )
    fixed_spec = replace(spec, fixed_parameter_names=frozenset({"synthetic_fixed"}))
    with pytest.raises(CompatibilityError, match="fixed parameters differ"):
        runtime_binding._instantiate_runtime_modules(
            fixed_refs,
            (fixed_refs[0],),
            {runtime_key: {"synthetic_fixed": "drifted"}},
            (fixed_spec,),
        )
    assert constructed == []

    monkeypatch.setattr(runtime_binding, "_verify_instance_contract", lambda *_args: None)
    with pytest.raises(CompatibilityError, match="closure is inconsistent"):
        runtime_binding._instantiate_runtime_modules(
            package.architecture.modules, runtime_modules[:-1], package.parameters, specs
        )
    assert constructed == []


def test_runtime_instance_contract_refuses_nonmodule_parameter_or_schema_drift(
    factory_request, architecture
) -> None:
    package, _decision, _envelope = _bound_package(factory_request, architecture)
    grouped_refs: dict[str, list[Any]] = {}
    for module_ref in package.architecture.modules:
        grouped_refs.setdefault(module_ref.runtime_key, []).append(module_ref)
    runtime_key, refs = next(iter(grouped_refs.items()))
    ref = refs[0]
    spec = next(
        item
        for item in _synthetic_specs(architecture)
        if runtime_binding._spec_matches_group(item, runtime_key, refs)
    )
    expected = package.parameters[runtime_key]
    instance = _fake_factory(spec, expected, architecture)

    with pytest.raises(CompatibilityError, match="lacks a manifest or parameters"):
        runtime_binding._verify_instance_contract(object(), spec, refs, expected)

    with pytest.raises(CompatibilityError, match="parameters differ"):
        runtime_binding._verify_instance_contract(instance, spec, refs, {"drift": True})

    changed_ref = replace(ref, parameter_schema={**dict(ref.parameter_schema), "unexpected": {}})
    with pytest.raises(CompatibilityError, match="manifest differs from package contract"):
        runtime_binding._verify_instance_contract(
            instance, spec, (changed_ref, *refs[1:]), expected
        )


@pytest.mark.parametrize(
    ("package_schema", "runtime_schema", "matches"),
    [
        ({}, {}, True),
        (None, {}, False),
        ({"risk": {}}, {}, False),
        ({"risk": None}, {"risk": {}}, False),
        ({"risk": {"minimum": "0.1"}}, {"risk": {"minimum": 0.1, "type": "number"}}, True),
        ({"risk": {"minimum": "bad"}}, {"risk": {"minimum": "0.1"}}, False),
        ({"risk": {"minimum": "0.1"}}, {"risk": {"minimum": "0.2"}}, False),
        (
            {"risk": {"minimum": "0.1", "exclusiveMinimum": True}},
            {"risk": {"minimum": "0.1", "exclusiveMinimum": False}},
            False,
        ),
    ],
)
def test_parameter_schema_compatibility_handles_normalization_and_refusal(
    package_schema, runtime_schema, matches
) -> None:
    assert runtime_binding._parameter_schemas_match(package_schema, runtime_schema) is matches


@pytest.mark.parametrize(
    ("reference", "lookup_result", "error"),
    [
        ("untrusted://evidence", {}, "not a Factory envelope"),
        (EVIDENCE_REFERENCE_PREFIX + "missing", LookupError("missing"), "unavailable"),
        (EVIDENCE_REFERENCE_PREFIX + "malformed", {1: "not-a-string-key"}, "malformed"),
    ],
)
def test_evidence_loader_refuses_untrusted_unavailable_or_malformed_values(
    reference, lookup_result, error
) -> None:
    def evidence_lookup(_reference):
        if isinstance(lookup_result, Exception):
            raise lookup_result
        return lookup_result

    binder = StrategyPackageRuntimeBinder(
        audit_lookup=AuditLookup(LookupError("unused")), evidence_lookup=evidence_lookup
    )
    with pytest.raises(CompatibilityError, match=error):
        binder._load_evidence(reference)


@pytest.mark.parametrize(
    ("import_path", "factory", "error"),
    [
        ("invalid", None, "import path is invalid"),
        ("trusted:target", None, "target is not callable"),
        ("trusted:target", ValueError("constructor failed"), "could not be constructed"),
        ("trusted:target", CompatibilityError("owner refusal"), "owner refusal"),
    ],
)
def test_trusted_constructor_resolution_fails_closed(
    monkeypatch, import_path, factory, error
) -> None:
    spec = SimpleNamespace(
        import_path=import_path,
        spec_id="synthetic_spec",
        expected_manifest_name="synthetic",
    )
    if import_path != "invalid":
        target = factory if isinstance(factory, Exception) else factory
        if target is None:
            module = SimpleNamespace(target=object())
        elif isinstance(target, Exception):

            def raise_failure(**_kwargs):
                raise target

            module = SimpleNamespace(target=raise_failure)
        else:
            module = SimpleNamespace(target=target)
        monkeypatch.setattr(runtime_binding.importlib, "import_module", lambda _name: module)
    with pytest.raises(CompatibilityError, match=error):
        runtime_binding._instantiate_trusted_spec(spec, {})


@pytest.mark.parametrize(
    "error",
    [
        EvidenceTamperedError("tampered history"),
        AuditUnavailableError("store unavailable"),
    ],
)
def test_preserves_fail_closed_audit_lookup_errors_without_writes(
    monkeypatch, factory_request, architecture, error
) -> None:
    package, decision, envelope = _bound_package(factory_request, architecture)
    lookup = AuditLookup(error)
    binder, _ = _binder(monkeypatch, architecture, decision, envelope, lookup=lookup)

    with pytest.raises(CompatibilityError, match="could not be verified"):
        binder.bind(package, expected_package_hash=package.package_hash)
    assert lookup.calls == [(decision.audit_id, decision.decision_hash)]
    assert lookup.write_calls == 0
