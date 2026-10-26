"""Code-owned mandatory runtime closure for MS5A multi-strategy research.

The v0.6 Strategy Factory catalog remains the searchable strategy surface.  This
module describes the separately governed runtime components that must accompany
every MS5A architecture at Research execution time. Entry Model already emits
the current `entry_model.plan` contract directly, so no legacy entry-plan bridge
is part of this closure. The closure supplies only the factual risk context,
canonical Router, and Router-to-Core order adapter that are absent from the
searchable v0.6 catalog.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import Any

from .backtest_config import TrustedRuntimeModule
from .canonical import canonical_sha256
from .errors import CompatibilityError
from .grammar import RuntimeFeatureContract

_MS5A_INSTRUMENT_POLICY = MappingProxyType(
    {
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "market_type": "linear_futures",
        "tick_size": 0.1,
        "quantity_step": 0.001,
        "min_quantity": 0.001,
        "min_notional": 5.0,
        "contract_size": 1.0,
        "price_precision": 1,
        "quantity_precision": 3,
    }
)
_INSTRUMENT_RISK_FIELDS = (
    "quantity_step",
    "min_quantity",
    "min_notional",
    "tick_size",
    "contract_size",
)


@dataclass(frozen=True, slots=True)
class MS5ARuntimeComponent:
    runtime_component_id: str
    runtime_component_version: str
    import_path: str
    source_commit_sha: str
    api_version: str
    state_schema_version: str
    required_features: tuple[str, ...]
    produced_features: tuple[str, ...]
    parameter_schema: Mapping[str, Mapping[str, Any]]
    fixed_parameters: Mapping[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.runtime_component_id, self.runtime_component_version

    @property
    def trusted_runtime_module(self) -> TrustedRuntimeModule:
        return TrustedRuntimeModule(
            runtime_component_id=self.runtime_component_id,
            runtime_component_version=self.runtime_component_version,
            import_path=self.import_path,
            source_commit_sha=self.source_commit_sha,
        )

    @property
    def feature_contract(self) -> RuntimeFeatureContract:
        return RuntimeFeatureContract(
            runtime_key=f"{self.runtime_component_id}@{self.runtime_component_version}",
            required_features=self.required_features,
            produced_features=self.produced_features,
        )

    @property
    def blueprint(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "runtime_component_id": self.runtime_component_id,
                "runtime_component_version": self.runtime_component_version,
                "import_path": self.import_path,
                "fixed_parameters": self.fixed_parameters,
                "parameter_schema": self.parameter_schema,
                "api_version": self.api_version,
                "state_schema_version": self.state_schema_version,
                "source_commit_sha": self.source_commit_sha,
            }
        )


@dataclass(frozen=True, slots=True)
class MS5ARuntimeClosure:
    components: tuple[MS5ARuntimeComponent, ...]

    def __post_init__(self) -> None:
        if not self.components:
            raise CompatibilityError("MS5A runtime closure cannot be empty")
        keys = tuple(item.key for item in self.components)
        component_ids = tuple(item.runtime_component_id for item in self.components)
        if len(keys) != len(set(keys)) or len(component_ids) != len(set(component_ids)):
            raise CompatibilityError("MS5A runtime closure identities must be unique")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            tuple(
                {
                    "blueprint": item.blueprint,
                    "required_features": item.required_features,
                    "produced_features": item.produced_features,
                }
                for item in self.components
            )
        )

    @property
    def feature_contracts(self) -> tuple[RuntimeFeatureContract, ...]:
        return tuple(item.feature_contract for item in self.components)

    @property
    def trusted_runtime_modules(self) -> tuple[TrustedRuntimeModule, ...]:
        return tuple(item.trusted_runtime_module for item in self.components)

    @property
    def blueprint(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(item.blueprint for item in self.components)


def ms5a_instrument_policy() -> Mapping[str, Any]:
    """Return the single owner-backed instrument contract used by MS5A Backtest."""

    return MappingProxyType(dict(_MS5A_INSTRUMENT_POLICY))


def ms5a_instrument_risk_rules() -> Mapping[str, Any]:
    """Project the exact risk-rule subset accepted by ``InstrumentRiskRules``."""

    return MappingProxyType(
        {name: _MS5A_INSTRUMENT_POLICY[name] for name in _INSTRUMENT_RISK_FIELDS}
    )


def build_ms5a_runtime_closure(*, source_commit_sha: str) -> MS5ARuntimeClosure:
    """Load exact code-owned manifests for the mandatory owner-backed runtime closure."""

    _source_sha(source_commit_sha)
    specs: tuple[tuple[str, str, Mapping[str, Any]], ...] = (
        (
            "runtime_risk_context",
            "modular_strategy_risk_request_composer:RuntimeRiskContextModule",
            MappingProxyType({"instrument_rules": ms5a_instrument_risk_rules()}),
        ),
        (
            "trade_plan_router",
            "modular_strategy_trade_plan_router:TradePlanRouterModule",
            MappingProxyType({}),
        ),
        (
            "trade_plan_core_order",
            "modular_strategy_trade_plan_router:TradePlanCoreOrderModule",
            MappingProxyType({}),
        ),
    )
    components: list[MS5ARuntimeComponent] = []
    for expected_name, import_path, fixed_parameters in specs:
        runtime_class = _load_runtime_class(import_path)
        manifest = getattr(runtime_class, "manifest", None)
        if manifest is None or getattr(manifest, "name", None) != expected_name:
            raise CompatibilityError(f"MS5A runtime manifest identity mismatch: {expected_name}")
        parameter_schema = getattr(manifest, "parameters_schema", None)
        if not isinstance(parameter_schema, Mapping):
            raise CompatibilityError(f"MS5A runtime parameter schema is malformed: {expected_name}")
        unknown_fixed = sorted(set(fixed_parameters) - set(parameter_schema))
        if unknown_fixed:
            raise CompatibilityError(
                f"MS5A runtime fixed parameters are absent from schema: {expected_name}"
            )
        components.append(
            MS5ARuntimeComponent(
                runtime_component_id=expected_name,
                runtime_component_version=_text(
                    getattr(manifest, "version", None), f"{expected_name}.version"
                ),
                import_path=import_path,
                source_commit_sha=source_commit_sha,
                api_version=_text(
                    getattr(manifest, "api_version", None), f"{expected_name}.api_version"
                ),
                state_schema_version=_text(
                    getattr(manifest, "state_schema_version", None),
                    f"{expected_name}.state_schema_version",
                ),
                required_features=_string_tuple(
                    getattr(manifest, "required_features", None),
                    f"{expected_name}.required_features",
                ),
                produced_features=_string_tuple(
                    getattr(manifest, "produced_features", None),
                    f"{expected_name}.produced_features",
                ),
                parameter_schema=MappingProxyType(dict(parameter_schema)),
                fixed_parameters=fixed_parameters,
            )
        )
    return MS5ARuntimeClosure(tuple(components))


def _load_runtime_class(import_path: str) -> Any:
    module_name, separator, attribute = import_path.partition(":")
    if not separator or not module_name or not attribute:
        raise CompatibilityError("MS5A runtime import path is malformed")
    try:
        module = import_module(module_name)
        runtime_class = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise CompatibilityError(f"MS5A runtime component is unavailable: {import_path}") from exc
    if not isinstance(runtime_class, type):
        raise CompatibilityError(f"MS5A runtime import is not a class: {import_path}")
    return runtime_class


def _source_sha(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CompatibilityError("MS5A runtime source SHA must be lowercase hex")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"{name} must be a canonical non-blank string")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or any(not isinstance(item, str) or not item for item in value):
        raise CompatibilityError(f"{name} must be a tuple of feature names")
    return value


__all__ = [
    "MS5ARuntimeClosure",
    "MS5ARuntimeComponent",
    "build_ms5a_runtime_closure",
    "ms5a_instrument_policy",
    "ms5a_instrument_risk_rules",
]
