"""Strategy Package construction, content hash, and paper-only admission gate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from .canonical import canonical_sha256
from .contracts import (
    AdmissionStatus,
    CandidateStrategy,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
    StrategyFactoryRequest,
    StrategyPackage,
    TimeRange,
)
from .errors import CompatibilityError

_ZERO_HASH = "0" * 64


class StrategyPackageBuilder:
    """Build an immutable package that can authorize paper, never live, execution."""

    def build(
        self,
        *,
        request: StrategyFactoryRequest,
        candidate: CandidateStrategy,
        strategy_id: str,
        version: str,
        commit_sha: str,
        research_periods: dict[str, TimeRange],
        robustness: RobustnessReport,
        holdout: HoldoutReport,
        key_results: dict[str, Any],
        known_weaknesses: tuple[str, ...],
        disabled_regimes: tuple[str, ...],
        evidence_links: tuple[str, ...],
        paper_configuration: dict[str, Any],
        risk_policy: dict[str, Any],
        execution_assumptions: dict[str, Any],
        audit_decision: StrategyAuditDecision | None = None,
        package_intent: Mapping[str, Any] | None = None,
    ) -> StrategyPackage:
        if candidate.evaluation is None:
            raise CompatibilityError("candidate has no evaluation")
        if robustness.candidate_id != candidate.candidate_id:
            raise CompatibilityError("robustness report belongs to another candidate")
        if holdout.candidate_id != candidate.candidate_id:
            raise CompatibilityError("holdout report belongs to another candidate")
        if holdout.dataset_fingerprint != request.dataset.fingerprint:
            raise CompatibilityError("holdout report uses a different dataset descriptor")
        expected_holdout = research_periods.get("holdout")
        if expected_holdout is None or expected_holdout != holdout.period:
            raise CompatibilityError("research periods must bind the holdout report")
        if request.explicit_holdout is not None and request.explicit_holdout != holdout.period:
            raise CompatibilityError("holdout report differs from the explicit holdout policy")
        if robustness.generated_at > holdout.generated_at:
            raise CompatibilityError("robustness evidence must precede holdout evidence")
        if audit_decision is not None:
            if audit_decision.candidate_id != candidate.candidate_id:
                raise CompatibilityError("audit decision belongs to another candidate")
            if audit_decision.issued_at < holdout.generated_at:
                raise CompatibilityError("audit decision must follow holdout evidence")
        _validate_research_periods(research_periods)
        _reject_secret_material(
            {
                "paper_configuration": paper_configuration,
                "risk_policy": risk_policy,
                "execution_assumptions": execution_assumptions,
            }
        )
        intent_context: Mapping[str, Any] | None = None
        intent_hash: str | None = None
        if package_intent is not None:
            if not isinstance(package_intent, Mapping):
                raise CompatibilityError("package_intent must be a mapping")
            intent_context = package_intent.get("request_context")
            intent_hash = package_intent.get("intent_hash")
            if not isinstance(intent_context, Mapping) or not isinstance(intent_hash, str):
                raise CompatibilityError("package_intent request context or hash is malformed")
            if (
                audit_decision is None
                or audit_decision.contract_version != "strategy-audit-port-v2"
            ):
                raise CompatibilityError("PackageIntent requires a v2 independent audit decision")
        elif (
            audit_decision is not None
            and audit_decision.contract_version == "strategy-audit-port-v2"
        ):
            raise CompatibilityError("v2 independent audit decision requires PackageIntent")
        prerequisite_gates_passed = (
            robustness.passed
            and holdout.passed
            and all(candidate.evaluation.constraints.values())
            and not candidate.evaluation.instability_flags
            and bool(evidence_links)
        )
        if audit_decision is None:
            admission = AdmissionStatus.INSUFFICIENT_EVIDENCE
        elif (
            audit_decision.status is AdmissionStatus.APPROVED_FOR_PAPER
            and prerequisite_gates_passed
        ):
            admission = AdmissionStatus.APPROVED_FOR_PAPER
        elif audit_decision.status is AdmissionStatus.REJECTED:
            admission = AdmissionStatus.REJECTED
        elif audit_decision.status is AdmissionStatus.INSUFFICIENT_EVIDENCE:
            admission = AdmissionStatus.INSUFFICIENT_EVIDENCE
        else:
            admission = AdmissionStatus.RESEARCH_ONLY
        constraints: dict[str, Any] = {
            "maximum_drawdown_fraction": request.maximum_drawdown_fraction,
            "minimum_trade_count": request.minimum_trade_count,
            "factory_request_fingerprint": request.fingerprint,
            "audit_id": (None if audit_decision is None else audit_decision.audit_id),
            "audit_decision_hash": (
                None if audit_decision is None else audit_decision.decision_hash
            ),
            "audit_status": (
                AdmissionStatus.INSUFFICIENT_EVIDENCE.value
                if audit_decision is None
                else audit_decision.status.value
            ),
            "candidate_id": candidate.candidate_id,
            "candidate_configuration_hash": candidate.configuration_hash,
            "audit_evidence_package_reference": (
                None if audit_decision is None else audit_decision.evidence_package_reference
            ),
            "audit_evidence_hash": (
                None if audit_decision is None else audit_decision.evidence_hash
            ),
            "audit_contract_version": (
                None if audit_decision is None else audit_decision.contract_version
            ),
        }
        if package_intent is not None:
            constraints["package_intent_hash"] = intent_hash
            constraints["package_intent_request"] = intent_context
        package = StrategyPackage(
            strategy_id=strategy_id,
            version=version,
            symbol=request.symbol,
            exchange=request.exchange,
            market_type=request.market_type,
            directions=candidate.architecture.directions,
            architecture=candidate.architecture,
            parameters=self._resolved_parameters(candidate),
            risk_policy=risk_policy,
            constraints=constraints,
            timeframes=request.allowed_timeframes,
            execution_assumptions=execution_assumptions,
            dataset_hashes=tuple(
                digest
                for digest in (
                    request.dataset.sha256,
                    request.dataset.funding_sha256,
                    request.dataset.exchange_info_sha256,
                )
                if digest is not None
            ),
            research_periods=research_periods,
            commit_sha=commit_sha,
            random_seeds=(request.random_seed,),
            key_results=key_results,
            robustness_report=robustness,
            holdout_report=holdout,
            known_weaknesses=known_weaknesses,
            disabled_regimes=disabled_regimes,
            admission_status=admission,
            evidence_links=evidence_links,
            paper_configuration={
                **paper_configuration,
                "execution_mode": "paper",
                "live_trading_authorized": False,
            },
            package_hash=_ZERO_HASH,
            live_trading_authorized=False,
        )
        package = replace(package, package_hash=self.compute_hash(package))
        if package_intent is not None:
            from .package_intent import verify_package_intent

            verify_package_intent(package_intent, package)
        return package

    @staticmethod
    def _resolved_parameters(candidate: CandidateStrategy) -> dict[str, dict[str, Any]]:
        resolved: dict[str, dict[str, Any]] = {
            module.runtime_key: dict(module.fixed_parameters)
            for module in candidate.architecture.runtime_modules
        }
        runtime_by_id = {
            module.runtime_component_id: module.runtime_key
            for module in candidate.architecture.runtime_modules
        }
        module_by_runtime = {
            module.runtime_key: module for module in candidate.architecture.runtime_modules
        }
        metadata = {
            "architecture_id": candidate.architecture.architecture_id,
            "architecture_fingerprint": candidate.architecture.fingerprint,
            "modules": tuple(item.key for item in candidate.architecture.modules),
            "parameters": dict(candidate.architecture.parameters),
        }
        for name, expected in metadata.items():
            if name in candidate.parameters:
                observed = candidate.parameters[name]
                if name == "modules":
                    observed = tuple(observed) if isinstance(observed, (list, tuple)) else observed
                if name == "parameters" and isinstance(observed, Mapping):
                    observed = dict(observed)
                if observed != expected:
                    raise CompatibilityError(
                        f"candidate research metadata conflicts with architecture: {name}"
                    )
        for identifier in ("factory_run_id", "factory_manifest_id"):
            if identifier in candidate.parameters:
                value = candidate.parameters[identifier]
                if not isinstance(value, str) or not value.strip():
                    raise CompatibilityError(
                        f"candidate research metadata is invalid: {identifier}"
                    )
        ignored_metadata = {
            "architecture_id",
            "architecture_fingerprint",
            "modules",
            "parameters",
            "factory_run_id",
            "factory_manifest_id",
        }
        for raw_name, value in candidate.parameters.items():
            name = str(raw_name)
            if name in ignored_metadata:
                continue
            component_id, separator, parameter = name.partition(".")
            if not separator or not parameter:
                raise CompatibilityError(f"candidate parameter is not runtime-qualified: {name}")
            runtime_key = runtime_by_id.get(component_id)
            if runtime_key is None:
                raise CompatibilityError(
                    f"candidate parameter references unknown runtime component: {name}"
                )
            module = module_by_runtime[runtime_key]
            if parameter not in module.parameter_schema:
                raise CompatibilityError(
                    f"candidate parameter is absent from runtime schema: {name}"
                )
            existing = resolved[runtime_key].get(parameter)
            if parameter in resolved[runtime_key] and existing != value:
                raise CompatibilityError(
                    f"candidate parameter conflicts with fixed configuration: {name}"
                )
            _validate_parameter_value(module.parameter_schema[parameter], value, name)
            resolved[runtime_key][parameter] = value
        return dict(sorted(resolved.items()))

    @staticmethod
    def candidate_parameters(package: StrategyPackage) -> dict[str, Any]:
        """Recover one exact frozen research configuration from a Strategy Package."""

        runtime_modules = package.architecture.runtime_modules
        runtime_by_key = {module.runtime_key: module for module in runtime_modules}
        component_ids = [module.runtime_component_id for module in runtime_modules]
        if len(component_ids) != len(set(component_ids)):
            raise CompatibilityError(
                "Strategy Package runtime component IDs are ambiguous across versions"
            )
        observed_keys = set(package.parameters)
        expected_keys = set(runtime_by_key)
        if observed_keys != expected_keys:
            missing = sorted(expected_keys - observed_keys)
            extra = sorted(observed_keys - expected_keys)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            raise CompatibilityError(
                "Strategy Package parameter modules differ from architecture: " + "; ".join(details)
            )
        flattened: dict[str, Any] = {}
        for runtime_key in sorted(runtime_by_key):
            module = runtime_by_key[runtime_key]
            raw_values = package.parameters[runtime_key]
            if not isinstance(raw_values, Mapping):
                raise CompatibilityError(
                    f"Strategy Package parameters must be a mapping: {runtime_key}"
                )
            values = dict(raw_values)
            unknown = sorted(set(values) - set(module.parameter_schema))
            if unknown:
                raise CompatibilityError(
                    f"Strategy Package has unknown runtime parameters for {runtime_key}: "
                    + ", ".join(unknown)
                )
            for fixed_name, fixed_value in module.fixed_parameters.items():
                if fixed_name not in values or values[fixed_name] != fixed_value:
                    raise CompatibilityError(
                        f"Strategy Package changed fixed parameter: "
                        f"{module.runtime_component_id}.{fixed_name}"
                    )
            for name, value in sorted(values.items()):
                _validate_parameter_value(
                    module.parameter_schema[name],
                    value,
                    f"{module.runtime_component_id}.{name}",
                )
                flattened[f"{module.runtime_component_id}.{name}"] = value
        return flattened

    def compute_hash(self, package: StrategyPackage) -> str:
        unsigned = replace(package, package_hash=_ZERO_HASH)
        return canonical_sha256(unsigned)

    def verify(self, package: StrategyPackage) -> None:
        if package.live_trading_authorized:
            raise CompatibilityError("live authorization is forbidden")
        if package.paper_configuration.get("execution_mode") != "paper":
            raise CompatibilityError("Strategy Package execution mode must be paper")
        if package.package_hash != self.compute_hash(package):
            raise CompatibilityError("Strategy Package hash mismatch")


def _validate_research_periods(periods: dict[str, TimeRange]) -> None:
    required = ("train", "validation", "test", "holdout")
    missing = [name for name in required if name not in periods]
    if missing:
        raise CompatibilityError("research periods are incomplete: " + ", ".join(missing))
    ordered = tuple(periods[name] for name in required)
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.end > right.start:
            raise CompatibilityError("research periods overlap or are out of order")


def _validate_parameter_value(schema: Any, value: Any, name: str) -> None:
    if not isinstance(schema, dict | Mapping):
        raise CompatibilityError(f"runtime parameter schema is invalid: {name}")
    enum_values = schema.get("enum")
    if enum_values is not None and value not in enum_values:
        raise CompatibilityError(f"candidate parameter is outside enum: {name}")
    kind = schema.get("type")
    if kind == "boolean" and not isinstance(value, bool):
        raise CompatibilityError(f"candidate parameter must be boolean: {name}")
    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise CompatibilityError(f"candidate parameter must be integer: {name}")
    if kind in {"number", "decimal"}:
        _numeric(value, name)
    if kind == "string" and not isinstance(value, str):
        raise CompatibilityError(f"candidate parameter must be string: {name}")
    if kind == "array" and not isinstance(value, list | tuple):
        raise CompatibilityError(f"candidate parameter must be an array: {name}")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None or maximum is not None:
        rendered = _numeric(value, name)
        if minimum is not None and rendered < _numeric(minimum, f"{name}.minimum"):
            raise CompatibilityError(f"candidate parameter is below minimum: {name}")
        if maximum is not None and rendered > _numeric(maximum, f"{name}.maximum"):
            raise CompatibilityError(f"candidate parameter is above maximum: {name}")


def _numeric(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise CompatibilityError(f"candidate parameter must be numeric: {name}")
    try:
        rendered = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CompatibilityError(f"candidate parameter must be numeric: {name}") from exc
    if not rendered.is_finite():
        raise CompatibilityError(f"candidate parameter must be finite: {name}")
    return rendered


def _reject_secret_material(value: Any, *, path: str = "package") -> None:
    secret_tokens = (
        "secret",
        "api_key",
        "apikey",
        "private_key",
        "access_token",
        "refresh_token",
        "password",
    )
    if isinstance(value, dict | Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(token in normalized for token in secret_tokens):
                raise CompatibilityError(f"secret-like field is forbidden in {path}: {key}")
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")
