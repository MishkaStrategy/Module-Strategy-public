"""Registered module metadata and compatibility-safe discovery."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .contracts import (
    Direction,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
)
from .errors import CompatibilityError, ContractError


@dataclass(frozen=True, slots=True)
class ModuleRegistrationMetadata:
    """Strategy Factory metadata absent from the generic Core manifest."""

    category: ModuleCategory
    import_path: str
    required_data: frozenset[str]
    supported_markets: frozenset[MarketType]
    supported_directions: frozenset[Direction]
    deterministic: bool
    test_coverage: Decimal
    maturity: ModuleMaturity
    compatibility_tags: frozenset[str] = frozenset()
    conflict_tags: frozenset[str] = frozenset()
    role_id: str | None = None
    runtime_role_categories: frozenset[ModuleCategory] = frozenset()
    role_facet: str | None = None
    fixed_parameters: Mapping[str, Any] = field(default_factory=dict)
    parameter_search_hints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    evidence_links: tuple[str, ...] = ()
    source_commit_sha: str | None = None


class StrategyModuleCatalog:
    """Immutable-identity registry for modules usable by Strategy Factory."""

    def __init__(self) -> None:
        self._by_key: dict[str, ModuleReference] = {}
        self._by_id: dict[str, dict[str, ModuleReference]] = defaultdict(dict)

    def register(self, reference: ModuleReference) -> None:
        """Register one exact module version, rejecting conflicting duplicates."""

        existing = self._by_key.get(reference.key)
        if existing is not None:
            if existing != reference:
                raise ContractError(f"conflicting registration for {reference.key}")
            return
        self._by_key[reference.key] = reference
        self._by_id[reference.module_id][reference.version] = reference

    def register_core_manifest(
        self,
        manifest: Any,
        metadata: ModuleRegistrationMetadata,
    ) -> ModuleReference:
        """Adapt a real Core ``ModuleManifest`` without extending Core itself."""

        required = (
            "name",
            "version",
            "api_version",
            "required_timeframes",
            "required_features",
            "produced_features",
            "parameters_schema",
            "state_schema_version",
        )
        missing = [name for name in required if not hasattr(manifest, name)]
        if missing:
            raise CompatibilityError(f"Core manifest is missing fields: {', '.join(missing)}")
        reference = ModuleReference(
            module_id=metadata.role_id or str(manifest.name),
            version=str(manifest.version),
            category=metadata.category,
            import_path=metadata.import_path,
            api_version=str(manifest.api_version),
            state_schema_version=str(manifest.state_schema_version),
            required_timeframes=tuple(str(item) for item in manifest.required_timeframes),
            required_features=tuple(str(item) for item in manifest.required_features),
            produced_features=tuple(str(item) for item in manifest.produced_features),
            optional_features=tuple(
                str(item) for item in getattr(manifest, "optional_features", ())
            ),
            fixed_parameters=metadata.fixed_parameters,
            parameter_search_hints=metadata.parameter_search_hints,
            evidence_links=metadata.evidence_links,
            source_commit_sha=metadata.source_commit_sha,
            required_data=metadata.required_data,
            supported_markets=metadata.supported_markets,
            supported_directions=metadata.supported_directions,
            deterministic=metadata.deterministic,
            test_coverage=metadata.test_coverage,
            maturity=metadata.maturity,
            compatibility_tags=metadata.compatibility_tags,
            conflict_tags=metadata.conflict_tags,
            parameter_schema=manifest.parameters_schema,
            runtime_component_id=str(manifest.name),
            runtime_component_version=str(manifest.version),
            runtime_role_categories=metadata.runtime_role_categories,
            role_facet=metadata.role_facet,
        )
        self.register(reference)
        return reference

    def register_module_instance(
        self,
        module: Any,
        metadata: ModuleRegistrationMetadata,
    ) -> ModuleReference:
        """Register a trusted installed module instance and pin its resolved parameters."""

        manifest = getattr(module, "manifest", None)
        if manifest is None:
            raise CompatibilityError("module instance has no manifest")
        parameters = getattr(module, "parameters", {})
        if not isinstance(parameters, Mapping):
            raise CompatibilityError("module instance parameters must be a mapping")
        resolved = dict(parameters)
        for name, expected in metadata.fixed_parameters.items():
            if name not in resolved or resolved[name] != expected:
                raise CompatibilityError(
                    f"module instance parameter {name!r} conflicts with registration metadata"
                )
        return self.register_core_manifest(manifest, metadata)

    def get(self, module_id: str, version: str | None = None) -> ModuleReference:
        """Get a specific version or the highest deterministic semantic version."""

        versions = self._by_id.get(module_id)
        if not versions:
            raise CompatibilityError(f"unknown module: {module_id}")
        if version is not None:
            try:
                return versions[version]
            except KeyError as exc:
                raise CompatibilityError(f"unknown module version: {module_id}@{version}") from exc
        return versions[sorted(versions, key=_version_key)[-1]]

    def by_category(self, category: ModuleCategory) -> tuple[ModuleReference, ...]:
        """Return a stable category view."""

        return tuple(
            sorted(
                (item for item in self._by_key.values() if item.category is category),
                key=lambda item: (item.module_id, _version_key(item.version)),
            )
        )

    def all(self) -> tuple[ModuleReference, ...]:
        """Return all exact registrations in deterministic order."""

        return tuple(sorted(self._by_key.values(), key=lambda item: item.key))

    def snapshot(self) -> Mapping[str, ModuleReference]:
        """Return a read-only exact-version snapshot."""

        return MappingProxyType(dict(sorted(self._by_key.items())))

    def require(self, keys: Iterable[str]) -> tuple[ModuleReference, ...]:
        """Resolve exact ``id@version`` keys."""

        output: list[ModuleReference] = []
        for key in keys:
            try:
                output.append(self._by_key[key])
            except KeyError as exc:
                raise CompatibilityError(f"required module is unavailable: {key}") from exc
        return tuple(output)


def _version_key(version: str) -> tuple[tuple[int, ...], str]:
    numeric, _, suffix = version.partition("-")
    try:
        parts = tuple(int(part) for part in numeric.split("."))
    except ValueError:
        return ((), version)
    return (parts, suffix)
