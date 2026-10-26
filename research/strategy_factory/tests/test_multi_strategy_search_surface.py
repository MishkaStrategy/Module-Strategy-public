from __future__ import annotations

from types import SimpleNamespace

from strategy_factory import multi_strategy_search_surface as search_surface
from strategy_factory.backtest_config import TrustedRuntimeModule
from strategy_factory.contracts import ModuleCategory
from strategy_factory.multi_strategy_owner import MS5A_ALLOWED_FAMILIES
from strategy_factory.multi_strategy_search_surface import (
    _catalog_families,
    build_ms5a_search_surface,
)


class _Catalog:
    def by_category(self, category: ModuleCategory):
        assert category is ModuleCategory.SIGNAL
        return (
            SimpleNamespace(
                module_id="breakout_return",
                compatibility_tags=frozenset(
                    {"strategy_family:breakout", "setup_profile:breakout_return_v1"}
                ),
            ),
            SimpleNamespace(
                module_id="liquidity_sweep",
                compatibility_tags=frozenset({"strategy_family:liquidity_sweep"}),
            ),
            SimpleNamespace(
                module_id="fvg_rt",
                compatibility_tags=frozenset({"strategy_family:fvg_rt"}),
            ),
        )


def test_catalog_family_identity_includes_signal_module_ids_and_tags() -> None:
    families = _catalog_families(_Catalog())

    assert {"breakout_return", "liquidity_sweep", "fvg_rt"}.issubset(families)
    assert "breakout" in families


def test_build_search_surface_exposes_complete_owner_family_and_runtime_contract(
    monkeypatch,
) -> None:
    source_sha = "a" * 40
    modules = tuple(
        SimpleNamespace(
            module_id=family,
            compatibility_tags=frozenset({f"strategy_family:{family}"}),
            runtime_component_id=f"{family}.runtime",
            runtime_component_version="1.0.0",
            import_path=f"fixture:{family}",
            source_commit_sha=source_sha,
        )
        for family in sorted(MS5A_ALLOWED_FAMILIES)
    )

    class Catalog:
        def by_category(self, category: ModuleCategory):
            assert category is ModuleCategory.SIGNAL
            return modules

        def all(self):
            return modules

    catalog = Catalog()
    report = SimpleNamespace(complete=True, issues=(), catalog=catalog)

    class Builder:
        def __init__(self, *, source_commit_sha: str):
            assert source_commit_sha == source_sha

        def build(self, specs):
            assert tuple(specs) == ("fixture-spec",)
            return report

    closure_module = TrustedRuntimeModule(
        runtime_component_id="runtime-closure",
        runtime_component_version="1.0.0",
        import_path="fixture:runtime_closure",
        source_commit_sha=source_sha,
    )
    closure = SimpleNamespace(
        trusted_runtime_modules=(closure_module,),
        blueprint=({"runtime_component_id": "runtime-closure"},),
    )
    monkeypatch.setattr(search_surface, "TrustedRepositoryCatalogBuilder", Builder)
    monkeypatch.setattr(search_surface, "v06_trusted_specs", lambda: ("fixture-spec",))
    monkeypatch.setattr(
        search_surface,
        "build_ms5a_runtime_closure",
        lambda *, source_commit_sha: closure,
    )

    surface = build_ms5a_search_surface(source_commit_sha=source_sha)

    assert surface.source_commit_sha == source_sha
    assert surface.allowed_strategy_families == MS5A_ALLOWED_FAMILIES
    assert MS5A_ALLOWED_FAMILIES.issubset(_catalog_families(surface.catalog))
    assert surface.trusted_runtime_modules
    assert surface.runtime_closure.trusted_runtime_modules
    assert surface.runtime_closure.blueprint


def test_catalog_family_identity_rejects_noncanonical_family_tag() -> None:
    class Catalog:
        def by_category(self, category: ModuleCategory):
            assert category is ModuleCategory.SIGNAL
            return (
                SimpleNamespace(
                    module_id="signal",
                    compatibility_tags=frozenset({"strategy_family: padded "}),
                ),
            )

    import pytest

    from strategy_factory.errors import CompatibilityError

    with pytest.raises(CompatibilityError, match="invalid strategy-family tag"):
        search_surface._catalog_families(Catalog())


def test_trusted_runtime_modules_reject_incomplete_empty_and_conflicting_provenance() -> None:
    import pytest

    from strategy_factory.errors import CompatibilityError

    class Catalog:
        def __init__(self, modules):
            self._modules = modules

        def all(self):
            return self._modules

    incomplete = SimpleNamespace(
        runtime_component_id=None,
        runtime_component_version="1.0.0",
        import_path="fixture:runtime",
        source_commit_sha="a" * 40,
    )
    with pytest.raises(CompatibilityError, match="provenance is incomplete"):
        search_surface._trusted_runtime_modules(Catalog((incomplete,)))

    with pytest.raises(CompatibilityError, match="no executable runtime"):
        search_surface._trusted_runtime_modules(Catalog(()))

    first = SimpleNamespace(
        runtime_component_id="runtime",
        runtime_component_version="1.0.0",
        import_path="fixture:first",
        source_commit_sha="a" * 40,
    )
    second = SimpleNamespace(
        runtime_component_id="runtime",
        runtime_component_version="1.0.0",
        import_path="fixture:second",
        source_commit_sha="a" * 40,
    )
    with pytest.raises(CompatibilityError, match="provenance is inconsistent"):
        search_surface._trusted_runtime_modules(Catalog((first, second)))


def test_merge_runtime_modules_rejects_catalog_closure_conflict() -> None:
    import pytest

    from strategy_factory.errors import CompatibilityError

    catalog_module = TrustedRuntimeModule(
        runtime_component_id="runtime",
        runtime_component_version="1.0.0",
        import_path="fixture:catalog",
        source_commit_sha="a" * 40,
    )
    closure_module = TrustedRuntimeModule(
        runtime_component_id="runtime",
        runtime_component_version="1.0.0",
        import_path="fixture:closure",
        source_commit_sha="a" * 40,
    )
    closure = SimpleNamespace(trusted_runtime_modules=(closure_module,))

    with pytest.raises(CompatibilityError, match="closure conflicts"):
        search_surface._merge_runtime_modules((catalog_module,), closure)


def test_build_search_surface_rejects_incomplete_catalog_and_missing_family(
    monkeypatch,
) -> None:
    import pytest

    from strategy_factory.errors import CompatibilityError

    class Builder:
        def __init__(self, *, source_commit_sha: str):
            assert source_commit_sha == "a" * 40

        def build(self, specs):
            del specs
            return SimpleNamespace(
                complete=False,
                issues=(SimpleNamespace(spec_id="broken", reason_code="MODULE_UNAVAILABLE"),),
            )

    monkeypatch.setattr(search_surface, "TrustedRepositoryCatalogBuilder", Builder)
    monkeypatch.setattr(search_surface, "v06_trusted_specs", lambda: ())
    with pytest.raises(CompatibilityError, match="catalog is incomplete"):
        build_ms5a_search_surface(source_commit_sha="a" * 40)

    class MissingCatalog:
        def by_category(self, category: ModuleCategory):
            assert category is ModuleCategory.SIGNAL
            return (
                SimpleNamespace(
                    module_id="breakout_return",
                    compatibility_tags=frozenset({"strategy_family:breakout_return"}),
                ),
            )

    report = SimpleNamespace(complete=True, issues=(), catalog=MissingCatalog())

    class CompleteBuilder(Builder):
        def build(self, specs):
            del specs
            return report

    monkeypatch.setattr(search_surface, "TrustedRepositoryCatalogBuilder", CompleteBuilder)
    with pytest.raises(CompatibilityError, match="missing required families"):
        build_ms5a_search_surface(source_commit_sha="a" * 40)
