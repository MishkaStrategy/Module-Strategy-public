"""Versioned semantic binding between a Factory request and its Strategy Package."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from .contracts import StrategyFactoryRequest, StrategyPackage
from .errors import CompatibilityError

PACKAGE_INTENT_SCHEMA_VERSION = 1
_AUDIT_CONSTRAINT_KEYS = frozenset(
    {
        "audit_id",
        "audit_decision_hash",
        "audit_status",
        "audit_evidence_package_reference",
        "audit_evidence_hash",
        "audit_contract_version",
        "package_intent_hash",
        "package_intent_request",
        "package_intent_evidence_links",
    }
)
_GENERATED_AUDIT_LINK_PREFIXES = (
    "strategy_audit_envelope-",
    "final_audit-",
)
_SUPPORTED_OBJECTIVE_FIELDS = frozenset(
    {
        "strategy_version",
        "known_weaknesses",
        "disabled_regimes",
        "risk_policy",
        "execution_assumptions",
        "walk_forward",
        "cost_scenarios",
        "robustness_scenarios",
    }
)
_WALK_FORWARD_FIELDS = frozenset(
    {
        "mode",
        "train_seconds",
        "validation_seconds",
        "oos_seconds",
        "step_seconds",
        "purge_seconds",
        "embargo_seconds",
        "minimum_history_seconds",
    }
)
_RISK_OBJECTIVE_FIELDS = frozenset({"research_risk_fraction", "initial_capital"})
_EXECUTION_OBJECTIVE_FIELDS = frozenset({"exchange", "market_type"})
_COST_SCENARIO_ID = "cost-double-v1"
_COST_SCENARIO_MULTIPLIER = 2
_OBJECTIVE_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class PackageIntent:
    """Canonical pre-audit meaning of a package, excluding audit output fields."""

    schema_version: int
    request_fingerprint: str
    request_context: Mapping[str, Any]
    package_projection: Mapping[str, Any]
    intent_hash: str

    def as_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "request_fingerprint": self.request_fingerprint,
                "request_context": self.request_context,
                "package_projection": self.package_projection,
                "intent_hash": self.intent_hash,
            }
        )


def request_context(request: StrategyFactoryRequest) -> Mapping[str, Any]:
    """Return the typed v2 request projection; unsupported objective config blocks."""

    rendered = strict_json_loads(canonical_json_bytes(request))
    if not isinstance(rendered, dict):
        raise CompatibilityError("Factory request did not produce a canonical object")
    _reject_secret_material(rendered)
    dataset = rendered.get("dataset")
    if isinstance(dataset, Mapping):
        for name in ("uri", "funding_uri"):
            uri = dataset.get(name)
            if isinstance(uri, str):
                _validate_dataset_uri(uri)
    rendered["objectives"] = _validated_objectives(
        rendered["objectives"],
        exchange=rendered["exchange"],
        market_type=rendered["market_type"],
        research_risk_fraction=rendered["research_risk_fraction"],
        initial_capital=rendered["initial_capital"],
    )
    if canonical_sha256(rendered) != request.fingerprint:
        raise CompatibilityError("v2 request projection differs from canonical Factory request")
    return MappingProxyType(rendered)


def _validated_objectives(
    objectives: Mapping[str, Any],
    *,
    exchange: str,
    market_type: str,
    research_risk_fraction: Any,
    initial_capital: Any,
) -> dict[str, Any]:
    """Validate the current v2 objective subset without persisting free-form maps."""

    result: dict[str, Any] = {}
    for key, value in objectives.items():
        if not isinstance(key, str) or not key.strip():
            raise CompatibilityError("Factory objective names must be non-blank strings")
        _reject_secret_material({key: value}, path="Factory objectives")
        if key == "strategy_version":
            if not isinstance(value, str) or not value.strip():
                raise CompatibilityError("objectives.strategy_version must be a non-blank string")
            result[key] = value
        elif key in {"known_weaknesses", "disabled_regimes"}:
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise CompatibilityError(
                    f"objectives.{key} must be a sequence of non-blank strings"
                )
            result[key] = list(value)
        elif key == "walk_forward":
            result[key] = None if value is None else _validated_walk_forward(value)
        elif key in {"risk_policy", "execution_assumptions"}:
            allowed = (
                _RISK_OBJECTIVE_FIELDS if key == "risk_policy" else _EXECUTION_OBJECTIVE_FIELDS
            )
            mapping = _validated_scalar_mapping(value, f"objectives.{key}", allowed=allowed)
            expected = (
                {
                    "research_risk_fraction": research_risk_fraction,
                    "initial_capital": initial_capital,
                }
                if key == "risk_policy"
                else {"exchange": exchange, "market_type": market_type}
            )
            if any(
                canonical_sha256(mapping[field])
                != canonical_sha256(strict_json_loads(canonical_json_bytes(expected[field])))
                for field in mapping
            ):
                raise CompatibilityError(f"objectives.{key} conflicts with typed Factory request")
            result[key] = mapping
        elif key == "cost_scenarios":
            if not isinstance(value, (list, tuple)):
                raise CompatibilityError("objectives.cost_scenarios must be a sequence")
            result[key] = [
                _validated_cost_scenario(item, f"objectives.cost_scenarios[{index}]")
                for index, item in enumerate(value)
            ]
        elif key == "robustness_scenarios":
            if not isinstance(value, (list, tuple)) or value:
                raise CompatibilityError(
                    "objectives.robustness_scenarios has no supported typed owner contract in v2"
                )
            result[key] = []
        elif key not in _SUPPORTED_OBJECTIVE_FIELDS:
            if (
                not _OBJECTIVE_METRIC.fullmatch(key)
                or not isinstance(value, str)
                or value not in {"maximize", "minimize"}
            ):
                raise CompatibilityError(f"unsupported decision-relevant Factory objective: {key}")
            result[key] = value
    return result


def _validated_scalar_mapping(
    value: Any, path: str, *, allowed: frozenset[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError(f"{path} must be a mapping")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise CompatibilityError(f"{path} keys must be non-blank strings")
        if allowed is not None and key not in allowed:
            raise CompatibilityError(f"unsupported decision-relevant field {path}.{key}")
        _reject_secret_material({key: item}, path=path)
        if item is None or isinstance(item, (bool, int, float, str)):
            result[key] = item
        elif isinstance(item, Mapping) and set(item) == {"$decimal"}:
            decimal_value = item["$decimal"]
            if not isinstance(decimal_value, str) or not decimal_value:
                raise CompatibilityError(f"{path}.{key} decimal is malformed")
            result[key] = {"$decimal": decimal_value}
        else:
            raise CompatibilityError(f"{path}.{key} must be a JSON scalar")
    return result


def _validated_cost_scenario(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError(f"{path} must be a mapping")
    scenario = dict(value)
    if set(scenario) != {"scenario_id", "roles", "execution_cost_multiplier"}:
        raise CompatibilityError(f"{path} fields differ from supported cost-double contract")
    if scenario["scenario_id"] != _COST_SCENARIO_ID:
        raise CompatibilityError(f"{path}.scenario_id is unsupported")
    roles = scenario["roles"]
    if not isinstance(roles, (list, tuple)) or tuple(roles) != ("oos",):
        raise CompatibilityError(f"{path}.roles must be the supported OOS role")
    if scenario["execution_cost_multiplier"] != _COST_SCENARIO_MULTIPLIER:
        raise CompatibilityError(f"{path}.execution_cost_multiplier is unsupported")
    return {
        "scenario_id": _COST_SCENARIO_ID,
        "roles": ["oos"],
        "execution_cost_multiplier": _COST_SCENARIO_MULTIPLIER,
    }


def _validated_walk_forward(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - _WALK_FORWARD_FIELDS:
        raise CompatibilityError("objectives.walk_forward fields differ from v2 allowlist")
    result = dict(value)
    if "mode" in result and result["mode"] not in {"rolling", "anchored"}:
        raise CompatibilityError("objectives.walk_forward.mode is unsupported")
    for name, item in result.items():
        if name == "mode":
            continue
        positive = name in {"train_seconds", "validation_seconds", "oos_seconds", "step_seconds"}
        if isinstance(item, bool) or not isinstance(item, int) or item < (1 if positive else 0):
            raise CompatibilityError(f"objectives.walk_forward.{name} has invalid integer value")
    return result


def package_projection(
    package: StrategyPackage,
    *,
    request: StrategyFactoryRequest | None = None,
) -> Mapping[str, Any]:
    """Project all package meaning while removing only audit-generated values."""

    stored_request = package.constraints.get("package_intent_request")
    if request is None:
        if not isinstance(stored_request, Mapping):
            raise CompatibilityError("Strategy Package lacks its signed request context")
        context = dict(stored_request)
    else:
        context = dict(request_context(request))
        if stored_request is not None and dict(stored_request) != context:
            raise CompatibilityError(
                "Strategy Package request context differs from Factory request"
            )

    constraints = {
        key: value
        for key, value in package.constraints.items()
        if key not in _AUDIT_CONSTRAINT_KEYS
    }
    key_results = dict(package.key_results)
    # This is a derived display field, not pre-audit package meaning.
    key_results.pop("audit_status", None)
    audit_links = {
        package.constraints.get("audit_id"),
        package.constraints.get("audit_decision_hash"),
        package.constraints.get("audit_evidence_package_reference"),
        package.constraints.get("audit_artifact_id"),
    }
    pre_audit_links = tuple(
        sorted(
            link
            for link in package.evidence_links
            if link not in audit_links and not link.startswith(_GENERATED_AUDIT_LINK_PREFIXES)
        )
    )
    projection = {
        "schema_version": PACKAGE_INTENT_SCHEMA_VERSION,
        "request_context": context,
        "strategy_id": package.strategy_id,
        "version": package.version,
        "symbol": package.symbol,
        "exchange": package.exchange,
        "market_type": package.market_type,
        "directions": package.directions,
        "architecture": package.architecture,
        "parameters": package.parameters,
        "risk_policy": package.risk_policy,
        "constraints": constraints,
        "timeframes": package.timeframes,
        "execution_assumptions": package.execution_assumptions,
        "dataset_hashes": package.dataset_hashes,
        "research_periods": package.research_periods,
        "commit_sha": package.commit_sha,
        "random_seeds": package.random_seeds,
        "key_results": key_results,
        "robustness_report": package.robustness_report,
        "holdout_report": package.holdout_report,
        "known_weaknesses": package.known_weaknesses,
        "disabled_regimes": package.disabled_regimes,
        "evidence_links": pre_audit_links,
        "paper_configuration": package.paper_configuration,
    }
    return MappingProxyType(projection)


def create_package_intent(
    package: StrategyPackage,
    request: StrategyFactoryRequest,
) -> PackageIntent:
    """Create the immutable signed intent from the same materialized package fields."""

    context = request_context(request)
    if any(link.startswith(_GENERATED_AUDIT_LINK_PREFIXES) for link in package.evidence_links):
        raise CompatibilityError(
            "pre-audit package uses a reserved generated audit evidence-link prefix"
        )
    projection = package_projection(package, request=request)
    payload = {
        "schema_version": PACKAGE_INTENT_SCHEMA_VERSION,
        "request_fingerprint": request.fingerprint,
        "request_context": context,
        "package_projection": projection,
    }
    return PackageIntent(
        schema_version=PACKAGE_INTENT_SCHEMA_VERSION,
        request_fingerprint=request.fingerprint,
        request_context=context,
        package_projection=projection,
        intent_hash=canonical_sha256(payload),
    )


def verify_package_intent(
    intent: Mapping[str, Any],
    package: StrategyPackage,
) -> None:
    """Fail closed unless the signed intent is exactly the package projection."""

    verify_intent_mapping(intent)
    projection = intent["package_projection"]
    digest = intent["intent_hash"]
    if package.constraints.get("factory_request_fingerprint") != intent["request_fingerprint"]:
        raise CompatibilityError("Strategy Package request fingerprint differs from signed intent")
    observed_projection = package_projection(package)
    if canonical_sha256(observed_projection) != canonical_sha256(projection):
        differing = sorted(
            key
            for key in set(observed_projection) | set(projection)
            if canonical_sha256(observed_projection.get(key))
            != canonical_sha256(projection.get(key))
        )
        raise CompatibilityError(
            "Strategy Package differs from signed package intent: " + ", ".join(differing)
        )
    if package.constraints.get("package_intent_hash") != digest:
        raise CompatibilityError("Strategy Package intent hash differs from signed intent")


def verify_intent_mapping(intent: Mapping[str, Any]) -> None:
    if set(intent) != {
        "schema_version",
        "request_fingerprint",
        "request_context",
        "package_projection",
        "intent_hash",
    }:
        raise CompatibilityError("package intent fields differ from v2 contract")
    schema_version = intent.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PACKAGE_INTENT_SCHEMA_VERSION
    ):
        raise CompatibilityError("unsupported package intent schema")
    context = intent.get("request_context")
    projection = intent.get("package_projection")
    digest = intent.get("intent_hash")
    fingerprint = intent.get("request_fingerprint")
    if not isinstance(context, Mapping) or not isinstance(projection, Mapping):
        raise CompatibilityError("package intent context or projection is malformed")
    if not isinstance(digest, str) or not isinstance(fingerprint, str):
        raise CompatibilityError("package intent hashes are malformed")
    if canonical_sha256(context) != fingerprint:
        raise CompatibilityError("package intent request fingerprint mismatch")
    expected_payload = {
        "schema_version": PACKAGE_INTENT_SCHEMA_VERSION,
        "request_fingerprint": fingerprint,
        "request_context": context,
        "package_projection": projection,
    }
    if canonical_sha256(expected_payload) != digest:
        raise CompatibilityError("package intent hash mismatch")
    projection_schema = projection.get("schema_version")
    if isinstance(projection_schema, bool) or not isinstance(projection_schema, int):
        raise CompatibilityError("package intent projection schema is malformed")


def _reject_secret_material(value: Any, *, path: str = "Factory request") -> None:
    secret_tokens = (
        "secret",
        "api_key",
        "apikey",
        "private_key",
        "access_token",
        "refresh_token",
        "password",
    )
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(token in normalized for token in secret_tokens):
                raise CompatibilityError(f"secret-like field is forbidden in {path}: {key}")
            _reject_secret_material(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_material(item, path=f"{path}[{index}]")


def _validate_dataset_uri(uri: str) -> None:
    """Keep credential-bearing dataset references out of signed v2 intent."""

    try:
        parsed = urlsplit(uri)
        if parsed.username is not None or parsed.password is not None:
            raise CompatibilityError("dataset URI must not contain credentials")
        if any(
            _is_secret_name(name) for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise CompatibilityError("dataset URI must not contain credential query parameters")
    except ValueError as exc:
        raise CompatibilityError("dataset URI is malformed") from exc


def _is_secret_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(
        token in normalized
        for token in (
            "secret",
            "api_key",
            "apikey",
            "private_key",
            "access_token",
            "refresh_token",
            "password",
        )
    )


__all__ = [
    "PACKAGE_INTENT_SCHEMA_VERSION",
    "PackageIntent",
    "create_package_intent",
    "package_projection",
    "request_context",
    "verify_intent_mapping",
    "verify_package_intent",
]
