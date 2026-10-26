"""Trusted MS5A multi-strategy catalog and runtime surface."""

from __future__ import annotations

from dataclasses import dataclass

from .backtest_config import TrustedRuntimeModule
from .catalog_v06 import TrustedRepositoryCatalogBuilder, v06_trusted_specs
from .contracts import ModuleCategory
from .errors import CompatibilityError
from .multi_strategy_owner import MS5A_ALLOWED_FAMILIES
from .multi_strategy_runtime_closure import MS5ARuntimeClosure, build_ms5a_runtime_closure
from .registry import StrategyModuleCatalog

_STRATEGY_FAMILY_PREFIX = "strategy_family:"
MS5A_RUNTIME_DATA = frozenset({"instrument_rules", "portfolio_snapshot"})


@dataclass(frozen=True, slots=True)
class MS5ASearchSurface:
    catalog: StrategyModuleCatalog
    allowed_strategy_families: frozenset[str]
    trusted_runtime_modules: tuple[TrustedRuntimeModule, ...]
    runtime_closure: MS5ARuntimeClosure
    source_commit_sha: str


def _catalog_families(catalog: StrategyModuleCatalog) -> frozenset[str]:
    families: set[str] = set()
    for module in catalog.by_category(ModuleCategory.SIGNAL):
        families.add(module.module_id)
        for tag in module.compatibility_tags:
            if not tag.startswith(_STRATEGY_FAMILY_PREFIX):
                continue
            family = tag[len(_STRATEGY_FAMILY_PREFIX) :]
            if not family or family != family.strip():
                raise CompatibilityError("trusted catalog contains an invalid strategy-family tag")
            families.add(family)
    return frozenset(families)


def _trusted_runtime_modules(catalog: StrategyModuleCatalog) -> tuple[TrustedRuntimeModule, ...]:
    trusted: dict[tuple[str, str], TrustedRuntimeModule] = {}
    for module in catalog.all():
        component_id = module.runtime_component_id
        version = module.runtime_component_version
        source_commit_sha = module.source_commit_sha
        if component_id is None or version is None or source_commit_sha is None:
            raise CompatibilityError("trusted catalog runtime provenance is incomplete")
        candidate = TrustedRuntimeModule(
            runtime_component_id=component_id,
            runtime_component_version=version,
            import_path=module.import_path,
            source_commit_sha=source_commit_sha,
        )
        prior = trusted.get(candidate.key)
        if prior is not None and prior != candidate:
            raise CompatibilityError("trusted catalog runtime provenance is inconsistent")
        trusted[candidate.key] = candidate
    if not trusted:
        raise CompatibilityError("trusted catalog exposes no executable runtime component")
    return tuple(trusted[key] for key in sorted(trusted))


def _merge_runtime_modules(
    catalog_modules: tuple[TrustedRuntimeModule, ...],
    closure: MS5ARuntimeClosure,
) -> tuple[TrustedRuntimeModule, ...]:
    trusted = {item.key: item for item in catalog_modules}
    for candidate in closure.trusted_runtime_modules:
        prior = trusted.get(candidate.key)
        if prior is not None and prior != candidate:
            raise CompatibilityError("MS5A runtime closure conflicts with trusted catalog")
        trusted[candidate.key] = candidate
    return tuple(trusted[key] for key in sorted(trusted))


def build_ms5a_search_surface(*, source_commit_sha: str) -> MS5ASearchSurface:
    """Load the complete audited v0.6 catalog and bind the exact MS5A family set."""

    report = TrustedRepositoryCatalogBuilder(source_commit_sha=source_commit_sha).build(
        v06_trusted_specs()
    )
    if not report.complete:
        reasons = ",".join(sorted({f"{item.spec_id}:{item.reason_code}" for item in report.issues}))
        raise CompatibilityError("trusted MS5A catalog is incomplete: " + reasons)

    catalog_families = _catalog_families(report.catalog)
    missing = sorted(MS5A_ALLOWED_FAMILIES - catalog_families)
    if missing:
        raise CompatibilityError(
            "trusted MS5A catalog is missing required families: " + ", ".join(missing)
        )

    closure = build_ms5a_runtime_closure(source_commit_sha=source_commit_sha)
    return MS5ASearchSurface(
        catalog=report.catalog,
        allowed_strategy_families=MS5A_ALLOWED_FAMILIES,
        trusted_runtime_modules=_merge_runtime_modules(
            _trusted_runtime_modules(report.catalog),
            closure,
        ),
        runtime_closure=closure,
        source_commit_sha=source_commit_sha,
    )


__all__ = [
    "MS5A_RUNTIME_DATA",
    "MS5ASearchSurface",
    "build_ms5a_search_surface",
]
