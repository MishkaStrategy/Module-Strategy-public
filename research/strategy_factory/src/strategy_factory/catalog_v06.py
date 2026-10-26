"""Evidence-backed discovery of trusted v0.6 repository strategy modules.

The loader is intentionally code-defined and allowlisted. It never imports a path
supplied by a Strategy Factory YAML request and therefore cannot be used as an
arbitrary-code execution surface.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from .canonical import freeze
from .contracts import (
    Direction,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
)
from .errors import CompatibilityError, ContractError
from .registry import ModuleRegistrationMetadata, StrategyModuleCatalog


@dataclass(frozen=True, slots=True)
class ModuleRoleSpec:
    """One logical Strategy Grammar role exposed by a runtime component."""

    module_id: str
    category: ModuleCategory
    role_facet: str | None = None


@dataclass(frozen=True, slots=True)
class TrustedModuleSpec:
    """Code-owned import and registration recipe for one repository component."""

    spec_id: str
    import_path: str
    expected_manifest_name: str
    expected_manifest_version: str
    roles: tuple[ModuleRoleSpec, ...]
    constructor_parameters: Mapping[str, Any] = field(default_factory=dict)
    fixed_parameter_names: frozenset[str] = frozenset()
    parameter_search_hints: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    required_data: frozenset[str] = frozenset({"ohlcv"})
    supported_markets: frozenset[MarketType] = frozenset({MarketType.LINEAR_FUTURES})
    supported_directions: frozenset[Direction] = frozenset({Direction.LONG, Direction.SHORT})
    deterministic: bool = True
    compatibility_tags: frozenset[str] = frozenset()
    conflict_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "spec_id",
            "import_path",
            "expected_manifest_name",
            "expected_manifest_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ContractError(f"{name} must be a non-blank trimmed string")
        if not self.roles:
            raise ContractError("trusted module spec requires at least one role")
        if len({role.module_id for role in self.roles}) != len(self.roles):
            raise ContractError("trusted module spec contains duplicate role IDs")
        if ":" not in self.import_path or self.import_path.count(":") != 1:
            raise ContractError("trusted module import_path must use module:attribute")
        module_name, attribute = self.import_path.split(":", 1)
        if not module_name or not attribute:
            raise ContractError("trusted module import_path is incomplete")
        if not isinstance(self.deterministic, bool):
            raise ContractError("trusted module deterministic must be bool")
        if not self.supported_markets or not self.supported_directions:
            raise ContractError("trusted module must declare supported market and direction")
        object.__setattr__(
            self, "constructor_parameters", freeze(dict(self.constructor_parameters))
        )
        object.__setattr__(
            self,
            "parameter_search_hints",
            freeze({key: dict(value) for key, value in self.parameter_search_hints.items()}),
        )


@dataclass(frozen=True, slots=True)
class ModuleEvidenceRecord:
    """Exact-version CI/audit evidence used for final-candidate eligibility."""

    runtime_component_id: str
    runtime_component_version: str
    source_commit_sha: str
    test_coverage: Decimal
    maturity: ModuleMaturity
    evidence_links: tuple[str, ...]

    @property
    def runtime_key(self) -> str:
        return f"{self.runtime_component_id}@{self.runtime_component_version}"

    def __post_init__(self) -> None:
        for name in ("runtime_component_id", "runtime_component_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ContractError(f"{name} must be a non-blank trimmed string")
        if (
            not isinstance(self.source_commit_sha, str)
            or len(self.source_commit_sha) != 40
            or any(character not in "0123456789abcdef" for character in self.source_commit_sha)
        ):
            raise ContractError("source_commit_sha must be a lowercase 40-character Git SHA")
        if (
            not isinstance(self.test_coverage, Decimal)
            or not self.test_coverage.is_finite()
            or not Decimal("0") <= self.test_coverage <= Decimal("100")
        ):
            raise ContractError("test_coverage must be a finite Decimal in [0, 100]")
        if len(set(self.evidence_links)) != len(self.evidence_links):
            raise ContractError("module evidence links must be unique")
        if any(
            not isinstance(link, str) or not link.strip() or link != link.strip()
            for link in self.evidence_links
        ):
            raise ContractError("module evidence links must be non-blank trimmed strings")
        if self.maturity in {ModuleMaturity.PAPER_READY, ModuleMaturity.RELEASED}:
            if not self.evidence_links:
                raise ContractError("final-eligible module evidence requires evidence links")
            if self.test_coverage <= 0:
                raise ContractError("final-eligible module evidence requires positive coverage")


@dataclass(frozen=True, slots=True)
class CatalogBuildIssue:
    spec_id: str
    reason_code: str
    details: str


@dataclass(frozen=True, slots=True)
class V06CatalogBuildReport:
    catalog: StrategyModuleCatalog
    registered: tuple[ModuleReference, ...]
    issues: tuple[CatalogBuildIssue, ...]

    @property
    def complete(self) -> bool:
        return not self.issues


class TrustedRepositoryCatalogBuilder:
    """Load only predeclared repository modules and bind exact evidence records."""

    def __init__(self, *, source_commit_sha: str) -> None:
        if (
            not isinstance(source_commit_sha, str)
            or len(source_commit_sha) != 40
            or any(character not in "0123456789abcdef" for character in source_commit_sha)
        ):
            raise ContractError("catalog source_commit_sha must be a lowercase Git SHA")
        self._source_commit_sha = source_commit_sha

    def build(
        self,
        specs: Sequence[TrustedModuleSpec],
        *,
        evidence: Mapping[str, ModuleEvidenceRecord] | None = None,
    ) -> V06CatalogBuildReport:
        records = evidence or {}
        catalog = StrategyModuleCatalog()
        registered: list[ModuleReference] = []
        issues: list[CatalogBuildIssue] = []
        used_evidence: set[str] = set()
        for spec in specs:
            try:
                instance = self._load_instance(spec)
                manifest = getattr(instance, "manifest", None)
                parameters = getattr(instance, "parameters", None)
                if manifest is None:
                    raise CompatibilityError("loaded module has no manifest")
                if not isinstance(parameters, Mapping):
                    raise CompatibilityError("loaded module has no resolved parameter mapping")
                self._validate_identity(spec, manifest)
                fixed = self._fixed_parameters(spec, parameters)
                runtime_key = f"{manifest.name}@{manifest.version}"
                record = records.get(runtime_key)
                if record is not None:
                    self._validate_evidence(runtime_key, record)
                    used_evidence.add(runtime_key)
                    coverage = record.test_coverage
                    maturity = record.maturity
                    evidence_links = record.evidence_links
                else:
                    coverage = Decimal("0")
                    maturity = ModuleMaturity.RESEARCH
                    evidence_links = ()
                role_categories = frozenset(role.category for role in spec.roles)
                for role in spec.roles:
                    metadata = ModuleRegistrationMetadata(
                        category=role.category,
                        import_path=spec.import_path,
                        required_data=spec.required_data,
                        supported_markets=spec.supported_markets,
                        supported_directions=spec.supported_directions,
                        deterministic=spec.deterministic,
                        test_coverage=coverage,
                        maturity=maturity,
                        compatibility_tags=spec.compatibility_tags,
                        conflict_tags=spec.conflict_tags,
                        role_id=role.module_id,
                        runtime_role_categories=(
                            role_categories if len(role_categories) > 1 else frozenset()
                        ),
                        role_facet=role.role_facet,
                        fixed_parameters=fixed,
                        parameter_search_hints=spec.parameter_search_hints,
                        evidence_links=evidence_links,
                        source_commit_sha=self._source_commit_sha,
                    )
                    registered.append(catalog.register_module_instance(instance, metadata))
            except Exception as exc:
                issues.append(
                    CatalogBuildIssue(
                        spec_id=spec.spec_id,
                        reason_code=self._reason_code(exc),
                        details=str(exc),
                    )
                )
        for runtime_key in sorted(set(records) - used_evidence):
            record = records[runtime_key]
            details = (
                f"unused evidence key {runtime_key}"
                if isinstance(record, ModuleEvidenceRecord)
                else f"invalid evidence record for {runtime_key}"
            )
            issues.append(
                CatalogBuildIssue(
                    spec_id=runtime_key,
                    reason_code="UNUSED_EVIDENCE",
                    details=details,
                )
            )
        return V06CatalogBuildReport(
            catalog=catalog,
            registered=tuple(registered),
            issues=tuple(issues),
        )

    @staticmethod
    def _load_instance(spec: TrustedModuleSpec) -> Any:
        module_name, attribute = spec.import_path.split(":", 1)
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
        if not callable(factory):
            raise CompatibilityError("trusted import attribute is not callable")
        return factory(**dict(spec.constructor_parameters))

    @staticmethod
    def _validate_identity(spec: TrustedModuleSpec, manifest: Any) -> None:
        if str(getattr(manifest, "name", "")) != spec.expected_manifest_name:
            raise CompatibilityError("manifest name does not match trusted specification")
        if str(getattr(manifest, "version", "")) != spec.expected_manifest_version:
            raise CompatibilityError("manifest version does not match trusted specification")

    @staticmethod
    def _fixed_parameters(
        spec: TrustedModuleSpec,
        parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        missing = sorted(spec.fixed_parameter_names - set(parameters))
        if missing:
            raise CompatibilityError(
                "manifest-shaping parameters are unavailable: " + ", ".join(missing)
            )
        return MappingProxyType(
            {name: parameters[name] for name in sorted(spec.fixed_parameter_names)}
        )

    def _validate_evidence(
        self,
        runtime_key: str,
        record: ModuleEvidenceRecord,
    ) -> None:
        if record.runtime_key != runtime_key:
            raise CompatibilityError("evidence runtime identity mismatch")
        if record.source_commit_sha != self._source_commit_sha:
            raise CompatibilityError("evidence commit does not match catalog source commit")

    @staticmethod
    def _reason_code(exc: Exception) -> str:
        if isinstance(exc, (ImportError, ModuleNotFoundError, AttributeError)):
            return "MODULE_UNAVAILABLE"
        if isinstance(exc, CompatibilityError):
            return "MODULE_CONTRACT_MISMATCH"
        if isinstance(exc, ContractError):
            return "MODULE_REGISTRATION_REJECTED"
        return "MODULE_INITIALIZATION_FAILED"


def v06_trusted_specs(
    *,
    base_timeframe: str = "5m",
    context_timeframe: str = "1h",
) -> tuple[TrustedModuleSpec, ...]:
    """Return the audited, code-owned v0.6 import allowlist.

    A missing package is reported rather than silently replaced. Breakout-return,
    liquidity-sweep and the owner-bound canonical FVG-RT composition have explicit
    setup-adapter profiles. Generic FVG remains a distinct research zone producer.
    """

    return (
        TrustedModuleSpec(
            spec_id="breakout_return",
            import_path="strategy_modules.breakout_return:BreakoutReturnModule",
            expected_manifest_name="breakout_return",
            expected_manifest_version="1.0.0",
            roles=(ModuleRoleSpec("breakout_return", ModuleCategory.SIGNAL),),
            fixed_parameter_names=frozenset({"reference_mode"}),
            parameter_search_hints={
                "lookback_bars": {"values": (12, 20, 36, 48, 72)},
                "breakout_buffer_bps": {"values": (0, 2, 5, 10, 20)},
                "min_return_depth": {"values": (0.01, 0.02, 0.05, 0.1)},
                "max_return_depth": {"values": (0.1, 0.2, 0.35, 0.5)},
                "max_return_bars": {"values": (6, 12, 24, 36, 72)},
            },
            compatibility_tags=frozenset(
                {"strategy_family:breakout", "setup_profile:breakout_return_v1"}
            ),
        ),
        TrustedModuleSpec(
            spec_id="liquidity_sweep",
            import_path="strategy_modules.liquidity_sweep:LiquiditySweepModule",
            expected_manifest_name="liquidity_sweep",
            expected_manifest_version="0.1.2",
            roles=(ModuleRoleSpec("liquidity_sweep", ModuleCategory.SIGNAL),),
            constructor_parameters={"timeframe": "base"},
            fixed_parameter_names=frozenset({"timeframe"}),
            parameter_search_hints={
                "pivot_left_bars": {"values": (2, 3, 5, 8)},
                "pivot_right_bars": {"values": (2, 3, 5, 8)},
                "min_penetration_bps": {"values": (0, 2, 5, 10)},
                "max_penetration_bps": {"values": (25, 50, 100, 200)},
                "min_wick_fraction": {"values": (0.25, 0.35, 0.5, 0.65)},
            },
            compatibility_tags=frozenset({"strategy_family:liquidity_sweep"}),
        ),
        TrustedModuleSpec(
            spec_id="fvg_rt",
            import_path="strategy_modules.fvg_rt:FvgRtCompositeModule",
            expected_manifest_name="fvg_rt_composite",
            expected_manifest_version="1.0.0",
            roles=(ModuleRoleSpec("fvg_rt", ModuleCategory.SIGNAL),),
            fixed_parameter_names=frozenset({"contract", "component_ids"}),
            compatibility_tags=frozenset({"strategy_family:fvg_rt"}),
        ),
        TrustedModuleSpec(
            spec_id="fvg",
            import_path="modular_strategy_fvg:FvgModule",
            expected_manifest_name="fvg",
            expected_manifest_version="0.3.0",
            roles=(ModuleRoleSpec("fvg", ModuleCategory.SIGNAL),),
            constructor_parameters={"timeframes": (base_timeframe,)},
            fixed_parameter_names=frozenset({"timeframes"}),
            parameter_search_hints={
                "direction_filter": {"values": ("both", "bullish", "bearish")},
                "gap_size_mode": {"values": ("legacy", "atr", "average_range")},
                "min_gap_size": {"values": ("0", "0.1", "0.25", "0.5")},
                "max_zone_age_bars": {"values": (0, 12, 36, 72, 144)},
            },
            compatibility_tags=frozenset({"strategy_family:fvg"}),
        ),
        TrustedModuleSpec(
            spec_id="market_regime",
            import_path="market_regime:MarketRegimeModule",
            expected_manifest_name="market_regime",
            expected_manifest_version="1.2.0",
            roles=(ModuleRoleSpec("market_regime", ModuleCategory.CONTEXT_FILTER),),
            constructor_parameters={"timeframe": context_timeframe},
            fixed_parameter_names=frozenset({"timeframe"}),
            parameter_search_hints={
                "fast_ema_period": {"values": (10, 20, 30, 50)},
                "slow_ema_period": {"values": (50, 100, 150, 200)},
                "atr_period": {"values": (10, 14, 20, 28)},
                "trend_efficiency_threshold": {"values": ("0.25", "0.35", "0.45", "0.55")},
            },
        ),
        TrustedModuleSpec(
            spec_id="wave_analysis",
            import_path="wave_analysis:WaveAnalysisModule",
            expected_manifest_name="wave_analysis",
            expected_manifest_version="0.2.0",
            roles=(ModuleRoleSpec("wave_analysis", ModuleCategory.CONTEXT_FILTER),),
            constructor_parameters={
                "timeframes": (context_timeframe,),
                "primary_timeframe": context_timeframe,
            },
            fixed_parameter_names=frozenset({"timeframes", "primary_timeframe"}),
            parameter_search_hints={
                "pivot_method": {"values": ("fractal", "percentage_zigzag", "atr_zigzag")},
                "left_bars": {"values": (2, 3, 5, 8)},
                "right_bars": {"values": (2, 3, 5, 8)},
                "atr_multiplier": {"values": ("1", "1.5", "2", "3")},
            },
        ),
        TrustedModuleSpec(
            spec_id="setup_adapter_breakout_return_v1",
            import_path="modular_setup_adapter:CanonicalSetupAdapterModule",
            expected_manifest_name="setup_adapter",
            expected_manifest_version="0.1.0",
            roles=(
                ModuleRoleSpec(
                    "setup_adapter.breakout_return_v1",
                    ModuleCategory.SETUP_ADAPTER,
                ),
            ),
            constructor_parameters={
                "sources": (
                    {
                        "feature_key": "breakout_return.confirmed",
                        "profile": "breakout_return_v1",
                        "source_module": "breakout_return",
                    },
                )
            },
            fixed_parameter_names=frozenset({"sources"}),
            compatibility_tags=frozenset({"setup_profile:breakout_return_v1"}),
        ),
        TrustedModuleSpec(
            spec_id="setup_adapter_liquidity_sweep_v1",
            import_path="modular_setup_adapter:CanonicalSetupAdapterModule",
            expected_manifest_name="setup_adapter",
            expected_manifest_version="0.1.0",
            roles=(
                ModuleRoleSpec(
                    "setup_adapter.liquidity_sweep_v1",
                    ModuleCategory.SETUP_ADAPTER,
                ),
            ),
            constructor_parameters={
                "sources": (
                    {
                        "feature_key": "liquidity_sweep.event",
                        "profile": "liquidity_sweep_v1",
                        "source_module": "liquidity_sweep",
                    },
                )
            },
            fixed_parameter_names=frozenset({"sources"}),
            compatibility_tags=frozenset({"setup_profile:liquidity_sweep_v1"}),
        ),
        TrustedModuleSpec(
            spec_id="setup_adapter_fvg_rt_composite_v1",
            import_path="modular_setup_adapter:CanonicalSetupAdapterModule",
            expected_manifest_name="setup_adapter",
            expected_manifest_version="0.1.0",
            roles=(
                ModuleRoleSpec(
                    "setup_adapter.fvg_rt_composite_v1",
                    ModuleCategory.SETUP_ADAPTER,
                ),
            ),
            constructor_parameters={
                "sources": (
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
            },
            fixed_parameter_names=frozenset({"sources"}),
            compatibility_tags=frozenset({"setup_profile:fvg_rt_composite_v1"}),
        ),
        TrustedModuleSpec(
            spec_id="entry_model",
            import_path="modular_modules.entry_model:EntryModelModule",
            expected_manifest_name="entry_model",
            expected_manifest_version="0.2.0",
            roles=(ModuleRoleSpec("entry_model", ModuleCategory.ENTRY),),
            parameter_search_hints={
                "default_entry_kind": {
                    "values": (
                        "market",
                        "limit_confirmation",
                        "limit_retracement",
                    )
                },
                "retracement_fraction": {"values": ("0.25", "0.5", "0.75")},
                "ttl_bars": {"values": (6, 12, 24, 36, 48, 72)},
            },
        ),
        TrustedModuleSpec(
            spec_id="protection_target",
            import_path=("modular_strategy_protection_target:ProtectionTargetModule"),
            expected_manifest_name="protection_target",
            expected_manifest_version="1.1.1",
            constructor_parameters={"entry_feature_key": "entry_model.plan"},
            roles=(
                ModuleRoleSpec(
                    "protection_target.protection",
                    ModuleCategory.PROTECTION,
                    role_facet="protection",
                ),
                ModuleRoleSpec(
                    "protection_target.target",
                    ModuleCategory.TARGET,
                    role_facet="target",
                ),
            ),
            fixed_parameter_names=frozenset(
                {
                    "entry_feature_key",
                    "invalidation_mode",
                    "invalidation_field",
                    "target_mode",
                    "structural_target_source",
                    "target_field",
                }
            ),
            parameter_search_hints={
                "fixed_stop_percent": {"values": (0.003, 0.005, 0.01, 0.02)},
                "stop_buffer_bps": {"values": (0, 2, 5, 10, 20)},
                "reward_r": {"values": (1.5, 2, 3, 4, 5, 6)},
                "max_stop_distance_bps": {"values": (100, 200, 300, 500, 1000)},
            },
        ),
        TrustedModuleSpec(
            spec_id="risk_request_composer",
            import_path=("modular_strategy_risk_request_composer:RiskRequestComposerModule"),
            expected_manifest_name="risk_request_composer",
            expected_manifest_version="0.1.0",
            roles=(
                ModuleRoleSpec(
                    "risk_request_composer",
                    ModuleCategory.RISK_REQUEST_ADAPTER,
                ),
            ),
            fixed_parameter_names=frozenset(
                {
                    "entry_feature_key",
                    "protection_feature_key",
                    "instrument_feature_key",
                    "portfolio_feature_key",
                    "require_instrument_feature",
                    "require_portfolio_feature",
                }
            ),
            required_data=frozenset({"ohlcv", "instrument_rules", "portfolio_snapshot"}),
        ),
        TrustedModuleSpec(
            spec_id="risk_sizing",
            import_path="strategy_modules.risk_sizing:RiskSizingModule",
            expected_manifest_name="risk_sizing",
            expected_manifest_version="1.2.0",
            roles=(ModuleRoleSpec("risk_sizing", ModuleCategory.RISK),),
            required_data=frozenset({"ohlcv", "instrument_rules", "portfolio_snapshot"}),
            parameter_search_hints={
                "default_risk_fraction": {"values": (0.001, 0.0025, 0.005, 0.01)},
                "max_trade_notional_multiple": {"values": (1, 2, 3, 5)},
                "max_portfolio_risk_fraction": {"values": (0.01, 0.02, 0.03)},
                "max_leverage": {"values": (1, 2, 3, 5)},
            },
        ),
        TrustedModuleSpec(
            spec_id="position_exit_management",
            import_path=("modular_strategy_position_exit_management:PositionExitManagementModule"),
            expected_manifest_name="position_exit_management",
            expected_manifest_version="0.2.1",
            roles=(
                ModuleRoleSpec(
                    "position_exit_management",
                    ModuleCategory.POSITION_MANAGEMENT,
                ),
            ),
            parameter_search_hints={
                "max_holding_minutes": {"values": (60, 120, 240, 480, 1440)},
                "trailing_mode": {"values": ("none", "r_multiple", "percent")},
                "breakeven_trigger_r": {"values": ("0.5", "1", "1.5", "2")},
            },
        ),
    )
