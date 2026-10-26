from __future__ import annotations

from decimal import Decimal
from types import ModuleType

import pytest
from modular_core import ModuleManifest

from strategy_factory.catalog_v06 import (
    ModuleEvidenceRecord,
    ModuleRoleSpec,
    TrustedModuleSpec,
    TrustedRepositoryCatalogBuilder,
    v06_trusted_specs,
)
from strategy_factory.contracts import (
    Direction,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
)


class FakeProtectionTarget:
    def __init__(self, *, mode: str = "fixed") -> None:
        self.parameters = {"mode": mode, "reward_r": 3}
        self.manifest = ModuleManifest(
            name="protection_target",
            version="1.1.1",
            api_version="1.0",
            required_features=("entry.plan",),
            produced_features=("protection.plan",),
            parameters_schema={
                "mode": {"type": "string", "enum": ("fixed", "structure")},
                "reward_r": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            state_schema_version="2",
        )


def spec() -> TrustedModuleSpec:
    return TrustedModuleSpec(
        spec_id="protection-target",
        import_path="fake_repo_module:FakeProtectionTarget",
        expected_manifest_name="protection_target",
        expected_manifest_version="1.1.1",
        roles=(
            ModuleRoleSpec(
                "protection_target.protection",
                ModuleCategory.PROTECTION,
                "protection",
            ),
            ModuleRoleSpec(
                "protection_target.target",
                ModuleCategory.TARGET,
                "target",
            ),
        ),
        constructor_parameters={"mode": "fixed"},
        fixed_parameter_names=frozenset({"mode"}),
        parameter_search_hints={"reward_r": {"values": (2, 3, 4)}},
        supported_markets=frozenset({MarketType.LINEAR_FUTURES}),
        supported_directions=frozenset({Direction.LONG, Direction.SHORT}),
    )


def test_trusted_catalog_registers_shared_runtime_with_exact_evidence(monkeypatch) -> None:
    fake_module = ModuleType("fake_repo_module")
    fake_module.FakeProtectionTarget = FakeProtectionTarget
    monkeypatch.setattr(
        "strategy_factory.catalog_v06.importlib.import_module",
        lambda name: fake_module,
    )
    commit = "a" * 40
    evidence = ModuleEvidenceRecord(
        runtime_component_id="protection_target",
        runtime_component_version="1.1.1",
        source_commit_sha=commit,
        test_coverage=Decimal("93.33"),
        maturity=ModuleMaturity.PAPER_READY,
        evidence_links=("ci://protection-target/coverage",),
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha=commit).build(
        (spec(),),
        evidence={evidence.runtime_key: evidence},
    )
    assert report.complete
    assert len(report.registered) == 2
    assert {item.category for item in report.registered} == {
        ModuleCategory.PROTECTION,
        ModuleCategory.TARGET,
    }
    assert {item.runtime_key for item in report.registered} == {"protection_target@1.1.1"}
    assert all(item.test_coverage == Decimal("93.33") for item in report.registered)
    assert all(item.fixed_parameters == {"mode": "fixed"} for item in report.registered)


def test_missing_evidence_downgrades_to_research(monkeypatch) -> None:
    fake_module = ModuleType("fake_repo_module")
    fake_module.FakeProtectionTarget = FakeProtectionTarget
    monkeypatch.setattr(
        "strategy_factory.catalog_v06.importlib.import_module",
        lambda name: fake_module,
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha="b" * 40).build((spec(),))
    assert report.complete
    assert all(item.maturity is ModuleMaturity.RESEARCH for item in report.registered)
    assert all(item.test_coverage == 0 for item in report.registered)


def test_stale_evidence_is_rejected(monkeypatch) -> None:
    fake_module = ModuleType("fake_repo_module")
    fake_module.FakeProtectionTarget = FakeProtectionTarget
    monkeypatch.setattr(
        "strategy_factory.catalog_v06.importlib.import_module",
        lambda name: fake_module,
    )
    evidence = ModuleEvidenceRecord(
        "protection_target",
        "1.1.1",
        "c" * 40,
        Decimal("90"),
        ModuleMaturity.PAPER_READY,
        ("ci://evidence",),
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha="d" * 40).build(
        (spec(),),
        evidence={evidence.runtime_key: evidence},
    )
    assert not report.registered
    assert report.issues[0].reason_code == "MODULE_CONTRACT_MISMATCH"


def test_unavailable_module_is_reported_not_replaced(monkeypatch) -> None:
    def missing(name: str):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(
        "strategy_factory.catalog_v06.importlib.import_module",
        missing,
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha="e" * 40).build((spec(),))
    assert not report.registered
    assert report.issues[0].reason_code == "MODULE_UNAVAILABLE"


def test_catalog_rejects_invalid_source_sha() -> None:
    with pytest.raises(Exception, match="source_commit_sha"):
        TrustedRepositoryCatalogBuilder(source_commit_sha="not-a-sha")


def test_unused_evidence_is_reported(monkeypatch) -> None:
    fake_module = ModuleType("fake_repo_module")
    fake_module.FakeProtectionTarget = FakeProtectionTarget
    monkeypatch.setattr(
        "strategy_factory.catalog_v06.importlib.import_module",
        lambda name: fake_module,
    )
    evidence = ModuleEvidenceRecord(
        "unrelated",
        "1.0.0",
        "f" * 40,
        Decimal("50"),
        ModuleMaturity.RESEARCH,
        ("ci://unrelated",),
    )
    report = TrustedRepositoryCatalogBuilder(source_commit_sha="f" * 40).build(
        (spec(),), evidence={evidence.runtime_key: evidence}
    )
    assert any(issue.reason_code == "UNUSED_EVIDENCE" for issue in report.issues)


def test_v06_catalog_declares_liquidity_sweep_setup_adapter() -> None:
    specs = {item.spec_id: item for item in v06_trusted_specs()}

    signal = specs["liquidity_sweep"]
    adapter = specs["setup_adapter_liquidity_sweep_v1"]

    assert "strategy_family:liquidity_sweep" in signal.compatibility_tags
    assert adapter.roles[0].module_id == "setup_adapter.liquidity_sweep_v1"
    assert adapter.constructor_parameters["sources"] == (
        {
            "feature_key": "liquidity_sweep.event",
            "profile": "liquidity_sweep_v1",
            "source_module": "liquidity_sweep",
        },
    )
    assert adapter.fixed_parameter_names == frozenset({"sources"})


def test_v06_catalog_declares_canonical_fvg_rt_composite_setup_adapter() -> None:
    specs = {item.spec_id: item for item in v06_trusted_specs()}

    signal = specs["fvg_rt"]
    adapter = specs["setup_adapter_fvg_rt_composite_v1"]
    generic_fvg = specs["fvg"]

    assert signal.import_path == "strategy_modules.fvg_rt:FvgRtCompositeModule"
    assert signal.expected_manifest_name == "fvg_rt_composite"
    assert signal.expected_manifest_version == "1.0.0"
    assert signal.roles[0].module_id == "fvg_rt"
    assert "strategy_family:fvg_rt" in signal.compatibility_tags
    assert signal.fixed_parameter_names == frozenset({"contract", "component_ids"})

    assert adapter.roles[0].module_id == "setup_adapter.fvg_rt_composite_v1"
    assert adapter.constructor_parameters["sources"] == (
        {
            "feature_key": "fvg_rt_final_15m.snapshot",
            "profile": "fvg_rt_final_15m_v1",
            "source_module": "fvg_rt_composite",
        },
        {
            "feature_key": "frozen_1h_short.snapshot",
            "profile": "fvg_rt_frozen_1h_v1",
            "source_module": "fvg_rt_composite",
        },
        {
            "feature_key": "frozen_4h_short.snapshot",
            "profile": "fvg_rt_frozen_4h_v1",
            "source_module": "fvg_rt_composite",
        },
    )
    assert adapter.fixed_parameter_names == frozenset({"sources"})
    assert generic_fvg.import_path == "modular_strategy_fvg:FvgModule"
    assert generic_fvg.expected_manifest_name == "fvg"


def test_v06_position_exit_search_hints_match_runtime_enum() -> None:
    specs = {item.spec_id: item for item in v06_trusted_specs()}
    position_exit = specs["position_exit_management"]

    assert position_exit.parameter_search_hints["trailing_mode"]["values"] == (
        "none",
        "r_multiple",
        "percent",
    )
