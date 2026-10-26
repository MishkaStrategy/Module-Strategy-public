"""Read-only binding of a Strategy Package to code-owned runtime modules.

This module intentionally stops short of runtime qualification: risk policy and
execution assumptions still need typed runtime-owner adapters before a caller can
use the returned modules to construct a PAPER runner.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from modular_core import ModuleManifest, StrategyModule
from strategy_audit_validation import AuditDecision, AuditOutcome

from .audit_envelope import (
    AUDIT_CONTRACT_VERSION,
    AUDIT_CONTRACT_VERSION_V2,
    EVIDENCE_REFERENCE_PREFIX,
)
from .canonical import canonical_sha256
from .catalog_v06 import TrustedModuleSpec, v06_trusted_specs
from .contracts import AdmissionStatus, CandidateStrategy, ModuleReference, StrategyPackage
from .errors import CompatibilityError
from .package import StrategyPackageBuilder

_SHA256_LENGTH = 64
_POLICY_BINDING_BLOCKERS = (
    "risk_policy has no typed TradingRunner owner adapter",
    "execution_assumptions have no typed TradingRunner owner adapter",
)
_V1_PACKAGE_BINDING_BLOCKERS = (
    "full package equivalence is incomplete: holdout metrics, key_results, known_weaknesses, "
    "disabled_regimes, and evidence_links are not all signed by the audit envelope",
    "authenticated audit evidence does not bind every package request/constraint field",
)


class StoredDecisionLookup(Protocol):
    def load_verified_stored_decision(
        self, *, audit_id: str, decision_hash: str
    ) -> AuditDecision: ...


class AuditEnvelopeLookup(Protocol):
    def __call__(self, evidence_package_reference: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PartialStrategyPackageRuntimeBinding:
    """Verified package/module binding that is explicitly not runner-qualified."""

    package_hash: str
    audit_id: str
    audit_decision_hash: str
    modules: tuple[Any, ...]
    runtime_keys: tuple[str, ...]
    qualification_complete: bool = False
    qualification_blockers: tuple[str, ...] = _POLICY_BINDING_BLOCKERS

    def require_runtime_qualified(self) -> None:
        raise CompatibilityError(
            "Strategy Package is only partially bound; runtime qualification is blocked: "
            + "; ".join(self.qualification_blockers)
        )


class StrategyPackageRuntimeBinder:
    """Bind package modules using only the repository's trusted module recipes.

    ``expected_package_hash`` and the audit identities are supplied by trusted
    deployment configuration. Package fields are data only; in particular their
    ``import_path`` values are compared with, never executed instead of, the
    code-owned ``TrustedModuleSpec`` allowlist.
    """

    def __init__(
        self,
        *,
        audit_lookup: StoredDecisionLookup,
        evidence_lookup: AuditEnvelopeLookup,
    ) -> None:
        if not callable(getattr(audit_lookup, "load_verified_stored_decision", None)):
            raise TypeError("audit_lookup must expose load_verified_stored_decision()")
        if not callable(evidence_lookup):
            raise TypeError("evidence_lookup must be callable")
        self._audit_lookup = audit_lookup
        self._evidence_lookup = evidence_lookup

    def bind(
        self, package: StrategyPackage, *, expected_package_hash: str
    ) -> PartialStrategyPackageRuntimeBinding:
        """Verify package/audit lineage and instantiate the exact trusted module graph.

        This is a read-only operation. The result cannot be treated as PAPER runtime
        approval until the named risk and execution adapters exist.
        """

        _digest(expected_package_hash, "expected_package_hash")
        builder = StrategyPackageBuilder()
        try:
            builder.verify(package)
        except Exception as exc:
            raise CompatibilityError("Strategy Package hash or PAPER contract is invalid") from exc
        if package.package_hash != expected_package_hash:
            raise CompatibilityError("Strategy Package hash differs from trusted configuration")
        if package.live_trading_authorized:
            raise CompatibilityError("Strategy Package cannot authorize LIVE trading")

        constraints = package.constraints
        declared_contract = constraints.get("audit_contract_version")
        if (
            declared_contract != AUDIT_CONTRACT_VERSION_V2
            and package.admission_status is not AdmissionStatus.APPROVED_FOR_PAPER
        ):
            raise CompatibilityError("Strategy Package is not approved for PAPER")
        if (
            declared_contract != AUDIT_CONTRACT_VERSION_V2
            and constraints.get("audit_status") != AdmissionStatus.APPROVED_FOR_PAPER.value
        ):
            raise CompatibilityError("Strategy Package audit constraint is not PAPER-approved")
        audit_id = _required_text(constraints.get("audit_id"), "constraints.audit_id")
        decision_hash = _digest(
            constraints.get("audit_decision_hash"), "constraints.audit_decision_hash"
        )
        if constraints.get("candidate_id") != package.paper_configuration.get("candidate_id"):
            raise CompatibilityError(
                "Strategy Package candidate identity is not consistently bound"
            )
        if constraints.get("audit_contract_version") is None:
            raise CompatibilityError("Strategy Package lacks original audit contract binding")
        if constraints.get("candidate_configuration_hash") is None:
            raise CompatibilityError("Strategy Package lacks candidate configuration binding")
        evidence_reference = _required_text(
            constraints.get("audit_evidence_package_reference"),
            "constraints.audit_evidence_package_reference",
        )
        evidence_hash = _digest(
            constraints.get("audit_evidence_hash"), "constraints.audit_evidence_hash"
        )

        try:
            decision = self._audit_lookup.load_verified_stored_decision(
                audit_id=audit_id,
                decision_hash=decision_hash,
            )
        except Exception as exc:
            raise CompatibilityError("bound stored audit decision could not be verified") from exc
        _verify_decision_identity(
            package,
            decision,
            audit_id=audit_id,
            decision_hash=decision_hash,
            evidence_reference=evidence_reference,
            evidence_hash=evidence_hash,
        )
        envelope = self._load_evidence(decision.evidence_package_reference)
        audit_binding_blockers = _verify_audit_envelope(package, envelope, decision)
        if package.admission_status is not AdmissionStatus.APPROVED_FOR_PAPER:
            raise CompatibilityError("Strategy Package is not approved for PAPER")
        if constraints.get("audit_status") != AdmissionStatus.APPROVED_FOR_PAPER.value:
            raise CompatibilityError("Strategy Package audit constraint is not PAPER-approved")
        _verify_decision_binding(
            package,
            decision,
            audit_id=audit_id,
            decision_hash=decision_hash,
            evidence_reference=evidence_reference,
            evidence_hash=evidence_hash,
        )

        runtime_modules = package.architecture.runtime_modules
        builder.candidate_parameters(package)
        modules = _instantiate_runtime_modules(
            package.architecture.modules,
            runtime_modules,
            package.parameters,
            v06_trusted_specs(),
        )
        return PartialStrategyPackageRuntimeBinding(
            package_hash=package.package_hash,
            audit_id=audit_id,
            audit_decision_hash=decision_hash,
            modules=modules,
            runtime_keys=tuple(item.runtime_key for item in runtime_modules),
            qualification_blockers=tuple(
                dict.fromkeys(
                    (
                        *_POLICY_BINDING_BLOCKERS,
                        *(
                            _V1_PACKAGE_BINDING_BLOCKERS
                            if decision.contract_version == AUDIT_CONTRACT_VERSION
                            else ()
                        ),
                        *audit_binding_blockers,
                    )
                )
            ),
        )

    def _load_evidence(self, evidence_reference: str) -> Mapping[str, Any]:
        if not evidence_reference.startswith(EVIDENCE_REFERENCE_PREFIX):
            raise CompatibilityError("stored audit evidence reference is not a Factory envelope")
        try:
            envelope = self._evidence_lookup(evidence_reference)
        except Exception as exc:
            raise CompatibilityError("bound audit evidence envelope is unavailable") from exc
        if not isinstance(envelope, Mapping) or any(not isinstance(key, str) for key in envelope):
            raise CompatibilityError("bound audit evidence envelope is malformed")
        return envelope


def _instantiate_runtime_modules(
    architecture_modules: Sequence[ModuleReference],
    runtime_modules: Sequence[ModuleReference],
    parameters: Mapping[str, Any],
    specs: Sequence[TrustedModuleSpec],
) -> tuple[Any, ...]:
    refs_by_runtime: dict[str, list[ModuleReference]] = {}
    for module in architecture_modules:
        refs_by_runtime.setdefault(module.runtime_key, []).append(module)
    if set(refs_by_runtime) != {item.runtime_key for item in runtime_modules}:
        raise CompatibilityError("Strategy Package runtime module closure is inconsistent")

    instances: list[Any] = []
    for representative in runtime_modules:
        runtime_key = representative.runtime_key
        refs = refs_by_runtime.get(runtime_key, [])
        matching = [spec for spec in specs if _spec_matches_group(spec, runtime_key, refs)]
        if len(matching) != 1:
            raise CompatibilityError(
                f"runtime component {runtime_key} has no unique trusted recipe"
            )
        spec = matching[0]
        raw_parameters = parameters.get(runtime_key)
        if not isinstance(raw_parameters, Mapping):
            raise CompatibilityError(f"runtime parameters are unavailable: {runtime_key}")
        module_parameters = dict(raw_parameters)
        fixed_parameters = dict(refs[0].fixed_parameters)
        if (
            set(fixed_parameters) != set(spec.fixed_parameter_names)
            or any(dict(item.fixed_parameters) != fixed_parameters for item in refs[1:])
            or any(module_parameters.get(key) != value for key, value in fixed_parameters.items())
        ):
            raise CompatibilityError(
                f"runtime fixed parameters differ from trusted recipe: {runtime_key}"
            )
        instance = _instantiate_trusted_spec(spec, module_parameters)
        _verify_instance_contract(instance, spec, refs, module_parameters)
        instances.append(instance)
    return tuple(instances)


def _spec_matches_group(
    spec: TrustedModuleSpec,
    runtime_key: str,
    refs: Sequence[ModuleReference],
) -> bool:
    if runtime_key != f"{spec.expected_manifest_name}@{spec.expected_manifest_version}":
        return False
    expected_roles = {(role.module_id, role.category, role.role_facet) for role in spec.roles}
    actual_roles = {(item.module_id, item.category, item.role_facet) for item in refs}
    profile_parameters_match = spec.expected_manifest_name != "setup_adapter"
    if spec.expected_manifest_name == "setup_adapter":
        if not refs or set(refs[0].fixed_parameters) != set(spec.fixed_parameter_names):
            return False
        profile_parameters_match = canonical_sha256(dict(spec.constructor_parameters)) == (
            canonical_sha256(
                {name: refs[0].fixed_parameters[name] for name in spec.fixed_parameter_names}
            )
        )
    return (
        expected_roles == actual_roles
        and bool(refs)
        # A shared setup_adapter runtime ID has several code-owned recipes. The
        # fixed source/profile mapping selects the exact recipe; it is not a
        # candidate-tunable field. Other fixed parameters, such as a configured
        # timeframe, remain bound by the signed architecture and package schema.
        and profile_parameters_match
        and all(item.import_path == spec.import_path for item in refs)
        and all(set(item.fixed_parameters) == set(spec.fixed_parameter_names) for item in refs)
        and all(
            canonical_sha256(item.parameter_search_hints)
            == canonical_sha256(spec.parameter_search_hints)
            for item in refs
        )
        and all(item.required_data == spec.required_data for item in refs)
        and all(
            item.runtime_role_categories == frozenset(role.category for role in spec.roles)
            for item in refs
        )
        and all(item.supported_markets == spec.supported_markets for item in refs)
        and all(item.supported_directions == spec.supported_directions for item in refs)
        and all(item.deterministic is spec.deterministic for item in refs)
        and all(item.compatibility_tags == spec.compatibility_tags for item in refs)
        and all(item.conflict_tags == spec.conflict_tags for item in refs)
    )


def _instantiate_trusted_spec(spec: TrustedModuleSpec, parameters: Mapping[str, Any]) -> Any:
    """Import only the repository-owned target declared by a trusted recipe."""

    module_name, separator, attribute = spec.import_path.partition(":")
    if not separator or not module_name or not attribute:
        raise CompatibilityError("trusted module recipe import path is invalid")
    constructor_values = dict(parameters)
    if (
        spec.spec_id == "risk_request_composer"
        and constructor_values.get("requested_risk_fraction") == ""
    ):
        # Its manifest serializes the unset optional Decimal as an empty string,
        # while its typed constructor accepts None. The instance reports the
        # manifest form again, so package comparison remains exact.
        constructor_values["requested_risk_fraction"] = None
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        if not callable(factory):
            raise CompatibilityError("trusted module recipe target is not callable")
        return factory(**constructor_values)
    except CompatibilityError:
        raise
    except Exception as exc:
        raise CompatibilityError(
            f"trusted module could not be constructed: {spec.expected_manifest_name}"
        ) from exc


def _verify_instance_contract(
    instance: Any,
    spec: TrustedModuleSpec,
    refs: Sequence[ModuleReference],
    expected_parameters: Mapping[str, Any],
) -> None:
    manifest = getattr(instance, "manifest", None)
    actual_parameters = getattr(instance, "parameters", None)
    if (
        not isinstance(instance, StrategyModule)
        or not isinstance(manifest, ModuleManifest)
        or not isinstance(actual_parameters, Mapping)
    ):
        raise CompatibilityError("trusted runtime module lacks a manifest or parameters")
    if (
        getattr(instance, "name", None) != manifest.name
        or str(getattr(manifest, "name", "")) != spec.expected_manifest_name
        or str(getattr(manifest, "version", "")) != spec.expected_manifest_version
    ):
        raise CompatibilityError("constructed runtime manifest identity differs from recipe")
    if dict(actual_parameters) != dict(expected_parameters):
        raise CompatibilityError("constructed runtime parameters differ from Strategy Package")
    for ref in refs:
        if (
            ref.api_version != str(getattr(manifest, "api_version", ""))
            or ref.state_schema_version != str(getattr(manifest, "state_schema_version", ""))
            or not _parameter_schemas_match(
                ref.parameter_schema, getattr(manifest, "parameters_schema", {})
            )
            or ref.required_features != tuple(getattr(manifest, "required_features", ()))
            or ref.optional_features != tuple(getattr(manifest, "optional_features", ()))
            or ref.produced_features != tuple(getattr(manifest, "produced_features", ()))
        ):
            raise CompatibilityError("constructed runtime manifest differs from package contract")


def _parameter_schemas_match(package_schema: object, runtime_schema: object) -> bool:
    if not isinstance(package_schema, Mapping) or not isinstance(runtime_schema, Mapping):
        return False
    if set(package_schema) != set(runtime_schema):
        return False
    for name, package_rules in package_schema.items():
        runtime_rules = runtime_schema.get(name)
        if not isinstance(package_rules, Mapping) or not isinstance(runtime_rules, Mapping):
            return False
        runtime_only_type = (
            "type" not in package_rules
            and runtime_rules.get("type") in {"number", "integer"}
            and bool({"minimum", "maximum"} & set(package_rules))
        )
        if set(package_rules) != set(runtime_rules) - (
            set() if not runtime_only_type else {"type"}
        ):
            return False
        for rule, package_value in package_rules.items():
            runtime_value = runtime_rules[rule]
            if rule in {"minimum", "maximum"}:
                try:
                    if Decimal(str(package_value)) != Decimal(str(runtime_value)):
                        return False
                except InvalidOperation:
                    return False
            elif package_value != runtime_value:
                return False
    return True


def _verify_decision_binding(
    package: StrategyPackage,
    decision: AuditDecision,
    *,
    audit_id: str,
    decision_hash: str,
    evidence_reference: str,
    evidence_hash: str,
) -> None:
    _verify_decision_identity(
        package,
        decision,
        audit_id=audit_id,
        decision_hash=decision_hash,
        evidence_reference=evidence_reference,
        evidence_hash=evidence_hash,
    )
    if decision.outcome is not AuditOutcome.APPROVED_FOR_PAPER:
        raise CompatibilityError("stored audit decision does not match package lineage")


def _verify_decision_identity(
    package: StrategyPackage,
    decision: AuditDecision,
    *,
    audit_id: str,
    decision_hash: str,
    evidence_reference: str,
    evidence_hash: str,
) -> None:
    constraints = package.constraints
    if (
        decision.audit_id != audit_id
        or decision.decision_hash != decision_hash
        or decision.live_trading_authorized
        or decision.candidate_id != constraints.get("candidate_id")
        or decision.evidence_package_reference != evidence_reference
        or decision.evidence_hash != evidence_hash
        or decision.contract_version != constraints.get("audit_contract_version")
        or constraints.get("candidate_configuration_hash") is None
    ):
        raise CompatibilityError("stored audit decision does not match package lineage")


def _verify_audit_envelope(
    package: StrategyPackage,
    envelope: Mapping[str, Any],
    decision: AuditDecision,
) -> tuple[str, ...]:
    if canonical_sha256(envelope) != decision.evidence_hash:
        raise CompatibilityError("stored audit envelope hash differs from signed evidence")
    contract_version = getattr(decision, "contract_version", AUDIT_CONTRACT_VERSION)
    if contract_version == AUDIT_CONTRACT_VERSION_V2:
        intent = envelope.get("package_intent")
        if not isinstance(intent, Mapping):
            raise CompatibilityError("v2 audit envelope lacks signed package intent")
        from .package_intent import verify_package_intent

        verify_package_intent(intent, package)
    elif envelope.get("schema_version") == 1:
        if package.constraints.get("package_intent_hash") is not None:
            raise CompatibilityError("v1 audit decision cannot bind a v2 package intent")
    else:
        raise CompatibilityError("stored audit envelope schema is unsupported")
    candidate = envelope.get("candidate")
    lineage = envelope.get("dataset_lineage")
    research = envelope.get("research")
    risk = envelope.get("risk_policy")
    execution = envelope.get("execution_assumptions")
    robustness = envelope.get("robustness")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(lineage, Mapping)
        or not isinstance(research, Mapping)
        or not isinstance(risk, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(robustness, Mapping)
    ):
        raise CompatibilityError("stored audit envelope is missing package lineage fields")

    constraints = package.constraints
    paper = package.paper_configuration
    if (
        candidate.get("candidate_id") != constraints.get("candidate_id")
        or candidate.get("configuration_hash") != constraints.get("candidate_configuration_hash")
        or research.get("candidate_id") != constraints.get("candidate_id")
        or research.get("configuration_hash") != constraints.get("candidate_configuration_hash")
    ):
        raise CompatibilityError("audit candidate lineage differs from package")
    raw_candidate_parameters = candidate.get("parameters")
    if not isinstance(raw_candidate_parameters, Mapping):
        raise CompatibilityError("audit candidate parameters are unavailable")
    expected_architecture_metadata = {
        "architecture_id": package.architecture.architecture_id,
        "architecture_fingerprint": package.architecture.fingerprint,
        "modules": tuple(item.key for item in package.architecture.modules),
        "parameters": dict(package.architecture.parameters),
    }
    for name, expected in expected_architecture_metadata.items():
        observed = raw_candidate_parameters.get(name)
        if name == "modules" and isinstance(observed, list):
            observed = tuple(observed)
        if name == "parameters" and isinstance(observed, Mapping):
            observed = dict(observed)
        if observed != expected:
            raise CompatibilityError(
                f"audit evidence does not bind package architecture metadata: {name}"
            )
    try:
        audited_candidate = CandidateStrategy(
            candidate_id=str(candidate["candidate_id"]),
            architecture=package.architecture,
            parameters=raw_candidate_parameters,
            configuration_hash=str(candidate["configuration_hash"]),
            lineage_references=(),
        )
        expected_parameters = StrategyPackageBuilder._resolved_parameters(audited_candidate)
    except Exception as exc:
        raise CompatibilityError("audit candidate does not match package architecture") from exc
    if canonical_sha256(expected_parameters) != canonical_sha256(package.parameters):
        raise CompatibilityError("package parameters differ from audited candidate resolution")
    run_id = _required_text(paper.get("factory_run_id"), "paper_configuration.factory_run_id")
    manifest_id = _required_text(
        paper.get("factory_manifest_id"), "paper_configuration.factory_manifest_id"
    )
    package_body = dict(envelope)
    evidence_package_id = package_body.pop("package_id", None)
    package_seed = {
        "factory_run_id": run_id,
        "factory_manifest_id": manifest_id,
        "factory_commit_sha": package.commit_sha,
        **package_body,
    }
    expected_evidence_package_id = f"sf-audit-{canonical_sha256(package_seed)[:24]}"
    if evidence_package_id != expected_evidence_package_id:
        raise CompatibilityError("audit evidence package identity differs from package lineage")

    expected_periods = {
        name: {"start": period.start, "end": period.end}
        for name, period in package.research_periods.items()
    }
    if canonical_sha256(research.get("periods")) != canonical_sha256(expected_periods):
        raise CompatibilityError("audit research periods differ from package")
    if tuple(research.get("random_seeds", ())) != package.random_seeds:
        raise CompatibilityError("audit random seeds differ from package")
    if (
        lineage.get("instrument") != package.symbol
        or lineage.get("exchange") != package.exchange
        or lineage.get("market_type") != package.market_type.value
        # dataset_hashes also carries auxiliary funding/exchange metadata. The
        # signed envelope's dataset lineage must bind the primary request dataset.
        or not package.dataset_hashes
        or lineage.get("dataset_sha256") != package.dataset_hashes[0]
        or not set(package.timeframes).issubset(set(lineage.get("timeframes", ())))
    ):
        raise CompatibilityError("audit dataset lineage differs from package")

    try:
        max_risk = Decimal(str(risk.get("maximum_risk_fraction")))
        package_risk = Decimal(str(package.risk_policy["research_risk_fraction"]))
        max_drawdown = Decimal(str(risk.get("maximum_drawdown_fraction")))
        package_drawdown = Decimal(str(constraints["maximum_drawdown_fraction"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise CompatibilityError("package risk policy is not audit-bindable") from exc
    if (
        not max_risk.is_finite()
        or not package_risk.is_finite()
        or not max_drawdown.is_finite()
        or not package_drawdown.is_finite()
        or risk.get("paper_only") is not True
        or max_risk != package_risk
        or max_drawdown != package_drawdown
    ):
        raise CompatibilityError("audit risk policy differs from package")
    expected_execution = {
        "mode": "paper",
        "exchange": package.exchange,
        "market_type": package.market_type.value,
        "live_trading_authorized": False,
        "private_exchange_access": False,
        "order_placement": False,
    }
    if any(
        (
            execution.get(name) is not value
            if isinstance(value, bool)
            else execution.get(name) != value
        )
        for name, value in expected_execution.items()
    ):
        raise CompatibilityError("audit execution assumptions differ from package")
    if (
        package.execution_assumptions.get("exchange") != package.exchange
        or package.execution_assumptions.get("market_type") != package.market_type.value
    ):
        raise CompatibilityError("package execution assumptions differ from audit evidence")
    if (
        robustness.get("candidate_id") != constraints.get("candidate_id")
        or robustness.get("report_sha256") != canonical_sha256(package.robustness_report)
        or robustness.get("passed") is not package.robustness_report.passed
    ):
        raise CompatibilityError("audit robustness evidence differs from package")
    unbound_risk = set(package.risk_policy) - {"research_risk_fraction"}
    unbound_execution = set(package.execution_assumptions) - {"exchange", "market_type"}
    if contract_version == AUDIT_CONTRACT_VERSION_V2:
        return (
            "independent candidate metric value source evidence is unavailable",
            "independent holdout metric value source evidence is unavailable",
        )
    if unbound_risk or unbound_execution or "minimum_trade_count" in constraints:
        return (
            "package contains risk/execution/request fields not represented by the audit envelope",
        )
    return ()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CompatibilityError(f"{name} must be a non-blank trimmed string")
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompatibilityError(f"{name} must be a lowercase SHA-256 digest")
    return value


__all__ = ["PartialStrategyPackageRuntimeBinding", "StrategyPackageRuntimeBinder"]
