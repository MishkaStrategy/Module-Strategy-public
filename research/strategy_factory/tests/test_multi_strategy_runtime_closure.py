from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import module
from modular_core import ModuleManifest

from strategy_factory import multi_strategy_runtime_closure as closure_module
from strategy_factory.catalog_v06 import v06_trusted_specs
from strategy_factory.contracts import Direction, ModuleCategory
from strategy_factory.errors import CompatibilityError
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.multi_strategy_runtime_closure import (
    MS5ARuntimeClosure,
    build_ms5a_runtime_closure,
    ms5a_instrument_policy,
    ms5a_instrument_risk_rules,
)
from strategy_factory.registry import StrategyModuleCatalog

_SOURCE_SHA = "a" * 40
_CLOSURE_IDS = (
    "runtime_risk_context",
    "trade_plan_router",
    "trade_plan_core_order",
)


def _build_closure(monkeypatch: pytest.MonkeyPatch) -> MS5ARuntimeClosure:
    manifests = {
        "modular_strategy_risk_request_composer:RuntimeRiskContextModule": ModuleManifest(
            name="runtime_risk_context",
            version="0.1.0",
            api_version="1.0",
            produced_features=("instrument.rules", "portfolio.risk_snapshot"),
            parameters_schema={"instrument_rules": {"default": None}},
            skip_until_ready=False,
        ),
        "modular_strategy_trade_plan_router:TradePlanRouterModule": ModuleManifest(
            name="trade_plan_router",
            version="0.1.0",
            api_version="1.0",
            required_features=(
                "entry_model.plan",
                "protection_target.plan",
                "risk.decision",
            ),
            produced_features=(
                "trade_plan_router.executable_plan",
                "trade_plan_router.rejected_plan",
                "trade_plan_router.routing_decision",
                "trade_plan_router.conflict_trace",
            ),
            skip_until_ready=False,
        ),
        "modular_strategy_trade_plan_router:TradePlanCoreOrderModule": ModuleManifest(
            name="trade_plan_core_order",
            version="0.1.0",
            api_version="1.0",
            required_features=("trade_plan_router.executable_plan",),
            produced_features=(),
            skip_until_ready=False,
        ),
    }

    def load(import_path: str) -> SimpleNamespace:
        return SimpleNamespace(manifest=manifests[import_path])

    monkeypatch.setattr(closure_module, "_load_runtime_class", load)
    return build_ms5a_runtime_closure(source_commit_sha=_SOURCE_SHA)


def test_runtime_closure_matches_current_direct_entry_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closure = _build_closure(monkeypatch)

    assert tuple(item.runtime_component_id for item in closure.components) == _CLOSURE_IDS
    assert all(item.source_commit_sha == _SOURCE_SHA for item in closure.components)
    assert closure.components[0].required_features == ()
    assert closure.components[0].produced_features == (
        "instrument.rules",
        "portfolio.risk_snapshot",
    )
    assert closure.components[1].required_features == (
        "entry_model.plan",
        "protection_target.plan",
        "risk.decision",
    )
    assert "trade_plan_router.executable_plan" in closure.components[1].produced_features
    assert closure.components[2].required_features == ("trade_plan_router.executable_plan",)
    assert closure.components[2].produced_features == ()
    assert "entry_plan_legacy_bridge" not in _CLOSURE_IDS


def test_runtime_closure_uses_exact_owner_instrument_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closure = _build_closure(monkeypatch)

    policy = ms5a_instrument_policy()
    risk_rules = ms5a_instrument_risk_rules()
    assert risk_rules == {
        "quantity_step": policy["quantity_step"],
        "min_quantity": policy["min_quantity"],
        "min_notional": policy["min_notional"],
        "tick_size": policy["tick_size"],
        "contract_size": policy["contract_size"],
    }
    assert closure.components[0].fixed_parameters["instrument_rules"] == risk_rules


def test_ms5a_closure_does_not_turn_runtime_components_into_search_roles() -> None:
    spec_ids = {item.spec_id for item in v06_trusted_specs()}

    assert spec_ids.isdisjoint(_CLOSURE_IDS)


def test_runtime_closure_fingerprint_changes_with_governed_risk_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closure = _build_closure(monkeypatch)
    changed_context = replace(
        closure.components[0],
        fixed_parameters={
            "instrument_rules": {
                **dict(ms5a_instrument_risk_rules()),
                "tick_size": 0.2,
            }
        },
    )
    changed = MS5ARuntimeClosure((changed_context, *closure.components[1:]))

    assert changed.fingerprint != closure.fingerprint


def _risk_chain_catalog() -> StrategyModuleCatalog:
    catalog = StrategyModuleCatalog()
    for item in (
        module("signal", ModuleCategory.SIGNAL, produced_features=("setup.confirmed",)),
        module(
            "entry",
            ModuleCategory.ENTRY,
            required_features=("setup.confirmed",),
            produced_features=("entry_model.plan",),
        ),
        module(
            "protection",
            ModuleCategory.PROTECTION,
            required_features=("entry_model.plan",),
            produced_features=("protection_target.plan",),
        ),
        module("target", ModuleCategory.TARGET),
        module(
            "risk_request_composer",
            ModuleCategory.RISK_REQUEST_ADAPTER,
            required_features=(
                "entry_model.plan",
                "protection_target.plan",
                "instrument.rules",
                "portfolio.risk_snapshot",
            ),
            produced_features=("risk.request",),
            required_data=frozenset({"ohlcv", "instrument_rules", "portfolio_snapshot"}),
        ),
        module(
            "risk_sizing",
            ModuleCategory.RISK,
            required_features=("risk.request",),
            produced_features=("risk.decision",),
        ),
    ):
        catalog.register(item)
    return catalog


def test_runtime_closure_closes_feature_chain_without_external_escape_hatch(
    descriptor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closure = _build_closure(monkeypatch)
    catalog = _risk_chain_catalog()

    blocked = StrategyGrammar(catalog).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        runtime_data=frozenset({"instrument_rules", "portfolio_snapshot"}),
    )
    assert not blocked.architectures
    assert any(
        "MISSING_FEATURE_DEPENDENCY" in rejection.reason_codes for rejection in blocked.rejections
    )

    accepted = StrategyGrammar(
        catalog,
        required_runtime_contracts=closure.feature_contracts,
    ).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        runtime_data=frozenset({"instrument_rules", "portfolio_snapshot"}),
    )
    assert len(accepted.architectures) == 1
    assert accepted.architectures[0].risk_adapters[0].module_id == "risk_request_composer"


def test_runtime_closure_rejects_empty_and_duplicate_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CompatibilityError, match="cannot be empty"):
        MS5ARuntimeClosure(())

    closure = _build_closure(monkeypatch)
    with pytest.raises(CompatibilityError, match="identities must be unique"):
        MS5ARuntimeClosure((closure.components[0], closure.components[0]))


@pytest.mark.parametrize("value", ("A" * 40, "a" * 39, "not-a-sha"))
def test_runtime_closure_rejects_invalid_source_sha(value: str) -> None:
    with pytest.raises(CompatibilityError, match="source SHA"):
        closure_module._source_sha(value)


@pytest.mark.parametrize(
    ("value", "helper", "match"),
    (
        (" padded ", "_text", "canonical non-blank"),
        ("", "_text", "canonical non-blank"),
        (["feature"], "_string_tuple", "tuple of feature names"),
        (("",), "_string_tuple", "tuple of feature names"),
    ),
)
def test_runtime_closure_scalar_contract_helpers_fail_closed(
    value,
    helper: str,
    match: str,
) -> None:
    function = getattr(closure_module, helper)
    with pytest.raises(CompatibilityError, match=match):
        function(value, "fixture")


def test_runtime_class_loader_rejects_malformed_missing_and_nonclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CompatibilityError, match="import path is malformed"):
        closure_module._load_runtime_class("not-a-runtime-path")

    def unavailable(name: str):
        raise ImportError(name)

    monkeypatch.setattr(closure_module, "import_module", unavailable)
    with pytest.raises(CompatibilityError, match="component is unavailable"):
        closure_module._load_runtime_class("missing.module:Runtime")

    monkeypatch.setattr(
        closure_module,
        "import_module",
        lambda name: SimpleNamespace(Runtime=object()),
    )
    with pytest.raises(CompatibilityError, match="import is not a class"):
        closure_module._load_runtime_class("fixture.module:Runtime")


@pytest.mark.parametrize(
    ("manifest", "match"),
    (
        (
            SimpleNamespace(
                name="wrong",
                version="1.0.0",
                api_version="1.0",
                state_schema_version="1",
                required_features=(),
                produced_features=(),
                parameters_schema={"instrument_rules": {}},
            ),
            "manifest identity mismatch",
        ),
        (
            SimpleNamespace(
                name="runtime_risk_context",
                version="1.0.0",
                api_version="1.0",
                state_schema_version="1",
                required_features=(),
                produced_features=(),
                parameters_schema=None,
            ),
            "parameter schema is malformed",
        ),
        (
            SimpleNamespace(
                name="runtime_risk_context",
                version="1.0.0",
                api_version="1.0",
                state_schema_version="1",
                required_features=(),
                produced_features=(),
                parameters_schema={},
            ),
            "fixed parameters are absent",
        ),
        (
            SimpleNamespace(
                name="runtime_risk_context",
                version=" ",
                api_version="1.0",
                state_schema_version="1",
                required_features=(),
                produced_features=(),
                parameters_schema={"instrument_rules": {}},
            ),
            "version must be a canonical non-blank",
        ),
        (
            SimpleNamespace(
                name="runtime_risk_context",
                version="1.0.0",
                api_version="1.0",
                state_schema_version="1",
                required_features=["bad"],
                produced_features=(),
                parameters_schema={"instrument_rules": {}},
            ),
            "required_features must be a tuple",
        ),
    ),
)
def test_runtime_closure_builder_rejects_manifest_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    manifest,
    match: str,
) -> None:
    monkeypatch.setattr(
        closure_module,
        "_load_runtime_class",
        lambda import_path: SimpleNamespace(manifest=manifest),
    )
    with pytest.raises(CompatibilityError, match=match):
        build_ms5a_runtime_closure(source_commit_sha=_SOURCE_SHA)
