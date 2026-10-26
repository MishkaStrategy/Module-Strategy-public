"""Declarative Strategy Grammar and deterministic architecture generation."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from .canonical import canonical_sha256
from .contracts import (
    DatasetDescriptor,
    Direction,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    StrategyArchitecture,
)
from .errors import CompatibilityError, ContractError
from .registry import StrategyModuleCatalog


@dataclass(frozen=True, slots=True)
class GrammarPolicy:
    """Limits and final-candidate safety policy for grammar generation."""

    version: str = "1.0"
    maximum_context_filters: int = 3
    maximum_setup_adapters: int = 2
    maximum_risk_adapters: int = 1
    maximum_position_management: int = 2
    maximum_portfolio_constraints: int = 1
    minimum_final_coverage: int = 85
    require_deterministic: bool = True
    require_stop: bool = True

    def __post_init__(self) -> None:
        for name in (
            "maximum_context_filters",
            "maximum_setup_adapters",
            "maximum_risk_adapters",
            "maximum_position_management",
            "maximum_portfolio_constraints",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractError(f"{name} must be a non-negative integer")
        if not 0 <= self.minimum_final_coverage <= 100:
            raise ContractError("minimum_final_coverage must be in [0, 100]")


@dataclass(frozen=True, slots=True)
class RuntimeFeatureContract:
    """Non-searchable runtime feature contract required around an architecture."""

    runtime_key: str
    required_features: tuple[str, ...] = ()
    produced_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.runtime_key, str)
            or not self.runtime_key.strip()
            or self.runtime_key != self.runtime_key.strip()
        ):
            raise ContractError("runtime feature contract key must be canonical non-blank text")
        for name in ("required_features", "produced_features"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(item, str) or not item.strip() or item != item.strip()
                for item in values
            ):
                raise ContractError(f"runtime feature contract {name} must be canonical strings")
            if len(values) != len(set(values)):
                raise ContractError(f"runtime feature contract {name} contains duplicates")
        if set(self.required_features) & set(self.produced_features):
            raise ContractError("runtime feature contract cannot require and produce one feature")


@dataclass(frozen=True, slots=True)
class ArchitectureRejection:
    reason_codes: tuple[str, ...]
    module_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchitectureGenerationResult:
    architectures: tuple[StrategyArchitecture, ...]
    rejections: tuple[ArchitectureRejection, ...]
    truncated: bool


class StrategyGrammar:
    """Generate only declarative architectures composed of registered modules."""

    def __init__(
        self,
        catalog: StrategyModuleCatalog,
        policy: GrammarPolicy | None = None,
        *,
        required_runtime_contracts: Iterable[RuntimeFeatureContract] = (),
    ) -> None:
        contracts = tuple(required_runtime_contracts)
        if any(not isinstance(item, RuntimeFeatureContract) for item in contracts):
            raise TypeError("required_runtime_contracts must contain RuntimeFeatureContract values")
        runtime_keys = tuple(item.runtime_key for item in contracts)
        if len(runtime_keys) != len(set(runtime_keys)):
            raise ContractError("required runtime feature contract keys must be unique")
        self._catalog = catalog
        self._policy = policy or GrammarPolicy()
        self._required_runtime_contracts = contracts

    def generate(
        self,
        *,
        dataset: DatasetDescriptor,
        directions: frozenset[Direction],
        forbidden_module_ids: frozenset[str] = frozenset(),
        maximum_architectures: int = 1_000,
        final_candidate: bool = False,
        allowed_signal_module_ids: frozenset[str] | None = None,
        external_features: frozenset[str] = frozenset(),
        runtime_data: frozenset[str] = frozenset(),
    ) -> ArchitectureGenerationResult:
        """Enumerate compatible architectures in stable content-hash order."""

        if not directions:
            raise ContractError("directions cannot be empty")
        if maximum_architectures <= 0:
            raise ContractError("maximum_architectures must be positive")
        available_data = self._available_data(dataset, runtime_data)
        category = self._eligible_by_category(
            dataset=dataset,
            directions=directions,
            available_data=available_data,
            forbidden_module_ids=forbidden_module_ids,
            final_candidate=final_candidate,
            allowed_signal_module_ids=allowed_signal_module_ids,
        )
        required = (
            ModuleCategory.SIGNAL,
            ModuleCategory.ENTRY,
            ModuleCategory.PROTECTION,
            ModuleCategory.TARGET,
            ModuleCategory.RISK,
        )
        missing = [item.value for item in required if not category[item]]
        if missing:
            raise CompatibilityError(f"no eligible modules for categories: {', '.join(missing)}")

        filters = _subsets(
            category[ModuleCategory.CONTEXT_FILTER], self._policy.maximum_context_filters
        )
        adapters = _subsets(
            category[ModuleCategory.SETUP_ADAPTER],
            self._policy.maximum_setup_adapters,
        )
        risk_adapters = _subsets(
            category[ModuleCategory.RISK_REQUEST_ADAPTER],
            self._policy.maximum_risk_adapters,
        )
        managers = _subsets(
            category[ModuleCategory.POSITION_MANAGEMENT],
            self._policy.maximum_position_management,
        )
        portfolio = _subsets(
            category[ModuleCategory.PORTFOLIO_CONSTRAINT],
            self._policy.maximum_portfolio_constraints,
        )
        accepted: list[StrategyArchitecture] = []
        rejected: list[ArchitectureRejection] = []
        truncated = False
        products = itertools.product(
            category[ModuleCategory.SIGNAL],
            filters,
            adapters,
            category[ModuleCategory.ENTRY],
            category[ModuleCategory.PROTECTION],
            category[ModuleCategory.TARGET],
            risk_adapters,
            category[ModuleCategory.RISK],
            managers,
            portfolio,
        )
        for (
            signal,
            context,
            setup_adapters,
            entry,
            protection,
            target,
            risk_adapters_selected,
            risk,
            management,
            constraints,
        ) in products:
            modules = (
                signal,
                *context,
                *setup_adapters,
                entry,
                protection,
                target,
                *risk_adapters_selected,
                risk,
                *management,
                *constraints,
            )
            reasons = self._combination_rejections(
                modules=modules,
                dataset=dataset,
                directions=directions,
                final_candidate=final_candidate,
                external_features=external_features,
                available_data=available_data,
            )
            if reasons:
                rejected.append(
                    ArchitectureRejection(
                        reason_codes=tuple(sorted(reasons)),
                        module_keys=tuple(item.key for item in modules),
                    )
                )
                continue
            identity = canonical_sha256(
                {
                    "grammar_version": self._policy.version,
                    "market_type": dataset.market_type,
                    "directions": directions,
                    "modules": tuple(
                        {
                            "role_key": item.key,
                            "runtime_key": item.runtime_key,
                            "category": item.category.value,
                            "role_facet": item.role_facet,
                        }
                        for item in modules
                    ),
                }
            )
            fixed_parameters = {
                module.runtime_key: dict(module.fixed_parameters)
                for module in modules
                if module.fixed_parameters
            }
            accepted.append(
                StrategyArchitecture(
                    architecture_id=f"arch-{identity[:24]}",
                    market_type=dataset.market_type,
                    directions=directions,
                    signal=signal,
                    context_filters=context,
                    setup_adapters=setup_adapters,
                    entry=entry,
                    protection=protection,
                    target=target,
                    risk_adapters=risk_adapters_selected,
                    risk=risk,
                    position_management=management,
                    portfolio_constraints=constraints,
                    parameters=fixed_parameters,
                    grammar_version=self._policy.version,
                )
            )
            if len(accepted) >= maximum_architectures:
                truncated = True
                break
        return ArchitectureGenerationResult(
            architectures=tuple(sorted(accepted, key=lambda item: item.architecture_id)),
            rejections=tuple(rejected),
            truncated=truncated,
        )

    def validate(
        self,
        architecture: StrategyArchitecture,
        *,
        dataset: DatasetDescriptor,
        final_candidate: bool = False,
        external_features: frozenset[str] = frozenset(),
        runtime_data: frozenset[str] = frozenset(),
    ) -> None:
        """Fail closed when an externally supplied architecture violates the grammar."""

        available_data = self._available_data(dataset, runtime_data)
        reasons = self._combination_rejections(
            modules=architecture.modules,
            dataset=dataset,
            directions=architecture.directions,
            final_candidate=final_candidate,
            external_features=external_features,
            available_data=available_data,
        )
        if architecture.market_type is not dataset.market_type:
            reasons.add("MARKET_MISMATCH")
        if reasons:
            raise CompatibilityError(
                "architecture rejected by grammar: " + ", ".join(sorted(reasons))
            )

    def _eligible_by_category(
        self,
        *,
        dataset: DatasetDescriptor,
        directions: frozenset[Direction],
        available_data: frozenset[str],
        forbidden_module_ids: frozenset[str],
        final_candidate: bool,
        allowed_signal_module_ids: frozenset[str] | None,
    ) -> dict[ModuleCategory, tuple[ModuleReference, ...]]:
        output: dict[ModuleCategory, list[ModuleReference]] = {
            category: [] for category in ModuleCategory
        }
        for module in self._catalog.all():
            if module.module_id in forbidden_module_ids:
                continue
            if (
                module.category is ModuleCategory.SIGNAL
                and allowed_signal_module_ids is not None
                and module.module_id not in allowed_signal_module_ids
            ):
                continue
            if dataset.market_type not in module.supported_markets:
                continue
            if not directions.issubset(module.supported_directions):
                continue
            if any(item not in dataset.available_timeframes for item in module.required_timeframes):
                continue
            if not module.required_data.issubset(available_data):
                continue
            if final_candidate and not self._is_final_eligible(module):
                continue
            output[module.category].append(module)
        return {key: tuple(value) for key, value in output.items()}

    def _combination_rejections(
        self,
        *,
        modules: Iterable[ModuleReference],
        dataset: DatasetDescriptor,
        directions: frozenset[Direction],
        final_candidate: bool,
        external_features: frozenset[str],
        available_data: frozenset[str],
    ) -> set[str]:
        module_tuple = tuple(modules)
        reasons: set[str] = set()
        conflicts = {tag for module in module_tuple for tag in module.conflict_tags}
        compatibility = {tag for module in module_tuple for tag in module.compatibility_tags}
        for module in module_tuple:
            if dataset.market_type not in module.supported_markets:
                reasons.add("UNSUPPORTED_MARKET")
            if not directions.issubset(module.supported_directions):
                reasons.add("DIRECTION_MISMATCH")
            if any(item not in dataset.available_timeframes for item in module.required_timeframes):
                reasons.add("TIMEFRAME_UNAVAILABLE")
            missing_data = module.required_data - available_data
            if missing_data:
                reasons.add("REQUIRED_DATA_MISSING")
                if "funding" in missing_data:
                    reasons.add("FUNDING_DATA_MISSING")
            if final_candidate and not self._is_final_eligible(module):
                reasons.add("MODULE_NOT_FINAL_ELIGIBLE")
        if conflicts & compatibility:
            reasons.add("CONFLICTING_MODULE_TAGS")
        keys = [module.key for module in module_tuple]
        if len(keys) != len(set(keys)):
            reasons.add("DUPLICATE_LOGICAL_MODULE_ROLE")
        runtime_versions: dict[str, set[str]] = {}
        runtime_groups: dict[str, list[ModuleReference]] = {}
        for module in module_tuple:
            runtime_component_id = module.runtime_component_id
            runtime_component_version = module.runtime_component_version
            if runtime_component_id is None or runtime_component_version is None:
                raise ContractError("runtime component identity is incomplete")
            runtime_versions.setdefault(runtime_component_id, set()).add(runtime_component_version)
            runtime_groups.setdefault(module.runtime_key, []).append(module)
        if any(len(versions) > 1 for versions in runtime_versions.values()):
            reasons.add("RUNTIME_COMPONENT_VERSION_CONFLICT")
        runtime_representatives = {
            runtime_key: references[0] for runtime_key, references in runtime_groups.items()
        }
        feature_contracts = {
            runtime_key: (module.required_features, module.produced_features)
            for runtime_key, module in runtime_representatives.items()
        }
        for contract in self._required_runtime_contracts:
            contract_features = (contract.required_features, contract.produced_features)
            prior_contract = feature_contracts.get(contract.runtime_key)
            if prior_contract is not None and prior_contract != contract_features:
                reasons.add("RUNTIME_COMPONENT_CONTRACT_CONFLICT")
                continue
            feature_contracts[contract.runtime_key] = contract_features
        feature_producers: dict[str, str] = {}
        duplicate_features: set[str] = set()
        for runtime_key, (_, produced_features) in feature_contracts.items():
            for feature in produced_features:
                prior = feature_producers.get(feature)
                if prior is not None and prior != runtime_key:
                    duplicate_features.add(feature)
                else:
                    feature_producers[feature] = runtime_key
        if duplicate_features:
            reasons.add("DUPLICATE_FEATURE_PRODUCER")
        dependency_edges: dict[str, set[str]] = {
            runtime_key: set() for runtime_key in feature_contracts
        }
        indegree = {runtime_key: 0 for runtime_key in feature_contracts}
        for runtime_key, (required_features, _) in feature_contracts.items():
            for feature in required_features:
                producer = feature_producers.get(feature)
                if producer is None:
                    if feature not in external_features:
                        reasons.add("MISSING_FEATURE_DEPENDENCY")
                    continue
                if producer == runtime_key:
                    reasons.add("SELF_FEATURE_DEPENDENCY")
                    continue
                if runtime_key not in dependency_edges[producer]:
                    dependency_edges[producer].add(runtime_key)
                    indegree[runtime_key] += 1
        ready = sorted(key for key, value in indegree.items() if value == 0)
        visited = 0
        while ready:
            current = ready.pop(0)
            visited += 1
            for consumer in sorted(dependency_edges[current]):
                indegree[consumer] -= 1
                if indegree[consumer] == 0:
                    ready.append(consumer)
                    ready.sort()
        if visited != len(feature_contracts):
            reasons.add("FEATURE_DEPENDENCY_CYCLE")
        for references in runtime_groups.values():
            if len(references) == 1:
                continue
            configurations = {canonical_sha256(dict(item.fixed_parameters)) for item in references}
            if len(configurations) > 1:
                reasons.add("RUNTIME_COMPONENT_CONFIGURATION_CONFLICT")
            categories = frozenset(item.category for item in references)
            if any(categories != item.runtime_role_categories for item in references):
                reasons.add("RUNTIME_COMPONENT_MULTIROLE_UNDECLARED")
                continue
            facets = [item.role_facet for item in references]
            if None in facets or len(facets) != len(set(facets)):
                reasons.add("RUNTIME_COMPONENT_ROLE_FACET_CONFLICT")
            contracts = {
                canonical_sha256(
                    {
                        "import_path": item.import_path,
                        "api_version": item.api_version,
                        "state_schema_version": item.state_schema_version,
                        "required_timeframes": item.required_timeframes,
                        "required_features": item.required_features,
                        "produced_features": item.produced_features,
                        "required_data": item.required_data,
                        "supported_markets": item.supported_markets,
                        "supported_directions": item.supported_directions,
                        "deterministic": item.deterministic,
                        "parameter_schema": item.parameter_schema,
                    }
                )
                for item in references
            }
            if len(contracts) != 1:
                reasons.add("RUNTIME_COMPONENT_CONTRACT_CONFLICT")
        if self._policy.require_stop and not any(
            module.category is ModuleCategory.PROTECTION for module in module_tuple
        ):
            reasons.add("STOP_REQUIRED")
        return reasons

    @staticmethod
    def _available_data(dataset: DatasetDescriptor, runtime_data: frozenset[str]) -> frozenset[str]:
        available = {"ohlcv", *runtime_data}
        if dataset.funding_sha256 is not None:
            available.add("funding")
        if dataset.exchange_info_sha256 is not None:
            available.add("instrument_rules")
        return frozenset(available)

    def _is_final_eligible(self, module: ModuleReference) -> bool:
        maturity_ok = module.maturity in {ModuleMaturity.PAPER_READY, ModuleMaturity.RELEASED}
        deterministic_ok = module.deterministic or not self._policy.require_deterministic
        coverage_ok = module.test_coverage >= self._policy.minimum_final_coverage
        return maturity_ok and deterministic_ok and coverage_ok


def _subsets(
    values: tuple[ModuleReference, ...], maximum_size: int
) -> tuple[tuple[ModuleReference, ...], ...]:
    output: list[tuple[ModuleReference, ...]] = [()]
    for size in range(1, min(maximum_size, len(values)) + 1):
        output.extend(itertools.combinations(values, size))
    return tuple(output)
