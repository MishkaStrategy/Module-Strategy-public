"""Concrete local handlers for the deterministic pre-execution research stages."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .adapters import (
    ResearchRunnerAdapter,
    ResearchRunnerExecutionAdapter,
    ResearchSplitPlan,
    ResultsAggregatorAdapter,
    StrategyAuditPort,
    StrategyPackageSourcePort,
)
from .audit_envelope import (
    AUDIT_CONTRACT_VERSION_V2,
    EVIDENCE_REFERENCE_PREFIX,
    AuditResearchPeriods,
    FactoryAuditEnvelopeBuilder,
    UnavailableDatasetAuditLineageSource,
    UnavailableResultsAuditEvidenceSource,
)
from .canonical import canonical_sha256
from .contract_codec import (
    decode_candidate_strategy,
    decode_experiment_plan,
    decode_holdout_report,
    decode_instrument_profile,
    decode_market_regime_definition,
    decode_robustness_report,
    decode_strategy_architecture,
    decode_strategy_audit_decision,
    decode_strategy_hypothesis,
    decode_strategy_package,
)
from .contracts import (
    AdmissionStatus,
    CandidateStrategy,
    Direction,
    ExperimentPlan,
    FactoryMode,
    FactoryStage,
    HoldoutReport,
    RobustnessReport,
    StrategyArchitecture,
    StrategyAuditDecision,
    StrategyFactoryRequest,
    StrategyHypothesis,
    StrategyPackage,
    TimeRange,
)
from .coordinator import FactoryStageContext, StageBudgetRequest, StageOutcome
from .errors import (
    CompatibilityError,
    ContractError,
    StageBlockedError,
)
from .hypothesis import HypothesisGenerator, RegimeLibrary
from .package import StrategyPackageBuilder
from .package_intent import create_package_intent
from .planner import ArchitecturePlanner
from .promotion import ResultsAggregatorCandidateSelector
from .ranking import RankedCandidate
from .registry import StrategyModuleCatalog
from .validation import CandidateGate, HoldoutAdmissionController


class RegimeDiscoveryHandler:
    """Create point-in-time regime definitions bound to the accepted profile."""

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        profile = decode_instrument_profile(
            context.load_artifact(FactoryStage.INSTRUMENT_PROFILE.value)
        )
        if profile.dataset_fingerprint != context.request.dataset.fingerprint:
            raise CompatibilityError("instrument profile belongs to another dataset")
        definitions = RegimeLibrary.default(context.request.allowed_timeframes)
        unavailable = sorted(
            {
                timeframe
                for definition in definitions
                for timeframe in definition.required_timeframes
                if timeframe not in context.request.dataset.available_timeframes
            }
        )
        if unavailable:
            raise CompatibilityError(
                "regime definitions require unavailable timeframes: " + ", ".join(unavailable)
            )
        return StageOutcome(
            artifact_type="regime_definitions",
            artifact={
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "dataset_fingerprint": profile.dataset_fingerprint,
                "definitions": definitions,
            },
            reason_codes=("POINT_IN_TIME_REGIMES_DEFINED",),
            subject_id=profile.profile_id,
        )


class HypothesisGenerationHandler:
    """Generate only falsifiable hypotheses backed by the accepted profile and catalog."""

    def __init__(self, catalog: StrategyModuleCatalog) -> None:
        self._generator = HypothesisGenerator(catalog)

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        profile = decode_instrument_profile(
            context.load_artifact(FactoryStage.INSTRUMENT_PROFILE.value)
        )
        regime_artifact = _mapping(
            context.load_artifact(FactoryStage.REGIME_DISCOVERY.value),
            "regime discovery artifact",
        )
        definitions = tuple(
            decode_market_regime_definition(item)
            for item in _sequence(regime_artifact.get("definitions"), "regime definitions")
        )
        if not definitions or any(not item.point_in_time for item in definitions):
            raise CompatibilityError("regime discovery has no causal definitions")
        if regime_artifact.get("profile_id") != profile.profile_id:
            raise CompatibilityError("regime discovery and profile identities differ")
        hypotheses = self._generator.generate(request=context.request, profile=profile)
        if not hypotheses:
            raise CompatibilityError(
                "no falsifiable hypothesis is eligible for the requested families"
            )
        return StageOutcome(
            artifact_type="strategy_hypotheses",
            artifact={
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "regime_ids": tuple(item.regime_id for item in definitions),
                "hypotheses": hypotheses,
            },
            reason_codes=("HYPOTHESES_GENERATED",),
            subject_id=profile.profile_id,
        )


_FAMILY_ARCHITECTURE_POLICY = "equal-family-architecture-capacity-v1"


def _family_architecture_limits(
    families: frozenset[str],
    maximum_architectures: int,
) -> tuple[dict[str, int], int]:
    """Allocate architecture slots equally across requested CREATE families."""

    if not families:
        raise ContractError("family-balanced architecture generation requires families")
    if (
        isinstance(maximum_architectures, bool)
        or not isinstance(maximum_architectures, int)
        or maximum_architectures <= 0
    ):
        raise ContractError("maximum_architectures must be a positive int")
    family_capacity = maximum_architectures // len(families)
    if family_capacity < 1:
        raise ContractError(
            "architecture budget cannot allocate one architecture to every strategy family"
        )
    limits = {family: family_capacity for family in sorted(families)}
    return limits, maximum_architectures - family_capacity * len(limits)


class ArchitectureGenerationHandler:
    """Generate bounded Strategy Grammar architectures from frozen hypotheses."""

    def __init__(
        self,
        planner: ArchitecturePlanner,
        *,
        directions: frozenset[Direction] = frozenset({Direction.LONG, Direction.SHORT}),
        external_features: frozenset[str] = frozenset(),
        runtime_data: frozenset[str] = frozenset(),
        final_candidate: bool = False,
        family_balanced: bool = False,
    ) -> None:
        if not directions:
            raise ContractError("architecture handler directions cannot be empty")
        if not isinstance(family_balanced, bool):
            raise ContractError("family_balanced must be boolean")
        self._planner = planner
        self._directions = directions
        self._external_features = external_features
        self._runtime_data = runtime_data
        self._final_candidate = final_candidate
        self._family_balanced = family_balanced

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.HYPOTHESIS_GENERATION.value),
            "hypothesis artifact",
        )
        hypotheses = tuple(
            decode_strategy_hypothesis(item)
            for item in _sequence(artifact.get("hypotheses"), "hypotheses")
        )
        family_allocations: tuple[dict[str, Any], ...] = ()
        unused_architecture_capacity = 0
        if (
            self._family_balanced
            and context.request.mode is FactoryMode.CREATE
            and len(context.request.allowed_strategy_families) > 1
        ):
            family_limits, remainder = _family_architecture_limits(
                context.request.allowed_strategy_families,
                context.request.budget.maximum_architectures,
            )
            architectures: list[StrategyArchitecture] = []
            rejections: list[Any] = []
            allocation_rows: list[dict[str, Any]] = []
            any_truncated = False
            for family in sorted(context.request.allowed_strategy_families):
                family_hypotheses = tuple(
                    hypothesis
                    for hypothesis in hypotheses
                    if any(module.module_id == family for module in hypothesis.module_candidates)
                )
                if not family_hypotheses:
                    raise CompatibilityError(
                        f"requested strategy family has no generated hypothesis: {family}"
                    )
                family_request = replace(
                    context.request,
                    allowed_strategy_families=frozenset({family}),
                    budget=replace(
                        context.request.budget,
                        maximum_architectures=family_limits[family],
                    ),
                )
                result = self._planner.generate_architectures(
                    request=family_request,
                    hypotheses=family_hypotheses,
                    directions=self._directions,
                    final_candidate=self._final_candidate,
                    external_features=self._external_features,
                    runtime_data=self._runtime_data,
                )
                if not result.architectures:
                    reason_codes = sorted(
                        {
                            reason
                            for rejection in result.rejections
                            for reason in rejection.reason_codes
                        }
                    )
                    raise CompatibilityError(
                        f"Strategy Grammar produced no architecture for family {family}; reasons="
                        + ",".join(reason_codes or ["NO_COMPATIBLE_COMBINATION"])
                    )
                if any(item.signal.module_id != family for item in result.architectures):
                    raise CompatibilityError(
                        "family-balanced architecture generation crossed SIGNAL family identity"
                    )
                architectures.extend(result.architectures)
                rejections.extend(result.rejections)
                any_truncated = any_truncated or result.truncated
                allocation_rows.append(
                    {
                        "family_id": family,
                        "family_capacity": family_limits[family],
                        "architecture_count": len(result.architectures),
                        "unused_capacity": (family_limits[family] - len(result.architectures)),
                    }
                )
            rendered_architectures = tuple(
                sorted(architectures, key=lambda item: item.architecture_id)
            )
            rendered_rejections = tuple(rejections)
            truncated = any_truncated
            family_allocations = tuple(allocation_rows)
            unused_architecture_capacity = remainder + sum(
                int(item["unused_capacity"]) for item in family_allocations
            )
        else:
            result = self._planner.generate_architectures(
                request=context.request,
                hypotheses=hypotheses,
                directions=self._directions,
                final_candidate=self._final_candidate,
                external_features=self._external_features,
                runtime_data=self._runtime_data,
            )
            if not result.architectures:
                reason_codes = sorted(
                    {reason for rejection in result.rejections for reason in rejection.reason_codes}
                )
                raise CompatibilityError(
                    "Strategy Grammar produced no architecture; reasons="
                    + ",".join(reason_codes or ["NO_COMPATIBLE_COMBINATION"])
                )
            rendered_architectures = result.architectures
            rendered_rejections = result.rejections
            truncated = result.truncated

        architecture_artifact: dict[str, Any] = {
            "schema_version": 1,
            "hypothesis_ids": tuple(item.hypothesis_id for item in hypotheses),
            "architectures": rendered_architectures,
            "rejections": rendered_rejections,
            "truncated": truncated,
        }
        reasons = [
            "ARCHITECTURES_GENERATED",
            "ARCHITECTURE_LIMIT_REACHED" if truncated else "ARCHITECTURE_SET_COMPLETE",
        ]
        if family_allocations:
            architecture_artifact.update(
                {
                    "family_architecture_policy": _FAMILY_ARCHITECTURE_POLICY,
                    "family_allocations": family_allocations,
                    "unused_architecture_capacity": unused_architecture_capacity,
                }
            )
            reasons.append("FAMILY_BALANCED_ARCHITECTURES")
        return StageOutcome(
            artifact_type="strategy_architectures",
            artifact=architecture_artifact,
            reason_codes=tuple(reasons),
            subject_id=context.checkpoint.run_id,
            architectures_generated=len(rendered_architectures),
        )


class PackageSeededHypothesisHandler:
    """Load and freeze an existing Strategy Package for adapt/improve modes."""

    def __init__(
        self,
        *,
        package_source: StrategyPackageSourcePort,
        package_builder: StrategyPackageBuilder | None = None,
    ) -> None:
        if not isinstance(package_source, StrategyPackageSourcePort):
            raise ContractError("package_source must implement StrategyPackageSourcePort")
        self._source = package_source
        self._builder = package_builder or StrategyPackageBuilder()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        del context
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        request = context.request
        if request.mode is FactoryMode.ADAPT:
            source_id = request.source_strategy_id
        elif request.mode is FactoryMode.IMPROVE:
            source_id = request.baseline_strategy_id
        else:
            raise CompatibilityError("package-seeded hypotheses support only adapt or improve mode")
        if source_id is None:
            raise CompatibilityError("package-seeded mode requires a source strategy ID")
        raw_package = self._source.load(source_id)
        package = (
            raw_package
            if isinstance(raw_package, StrategyPackage)
            else decode_strategy_package(raw_package)
        )
        self._builder.verify(package)
        if package.strategy_id != source_id:
            raise CompatibilityError("Strategy Package identity differs from requested source")
        if package.admission_status is AdmissionStatus.REJECTED:
            raise CompatibilityError("rejected Strategy Package cannot seed research")
        if package.exchange != request.exchange:
            raise CompatibilityError("Strategy Package exchange differs from request")
        if package.market_type is not request.market_type:
            raise CompatibilityError("Strategy Package market type differs from request")
        if request.mode is FactoryMode.IMPROVE and package.symbol != request.symbol:
            raise CompatibilityError("improve mode requires the baseline instrument")
        missing_timeframes = sorted(
            set(package.timeframes) - set(request.dataset.available_timeframes)
        )
        if missing_timeframes:
            raise CompatibilityError(
                "Strategy Package requires unavailable timeframes: " + ", ".join(missing_timeframes)
            )
        seed_parameters = self._builder.candidate_parameters(package)
        hypothesis = StrategyHypothesis(
            hypothesis_id="seeded-" + package.package_hash[:24],
            description=(
                f"{request.mode.value} the verified Strategy Package "
                f"{package.strategy_id} without losing its control arm"
            ),
            expected_edge="retain or improve robust out-of-sample performance",
            applicability=(request.symbol, request.market_type.value),
            risks=("baseline degradation", "selection bias", "complexity inflation"),
            module_candidates=(package.architecture.signal,),
            parameter_ranges=seed_parameters,
            falsification_criteria={
                "baseline_control_required": True,
                "no_live_authorization": True,
            },
            estimated_experiments=1,
            novelty=Decimal("0.05"),
            origin={
                "mode": request.mode.value,
                "source_strategy_id": package.strategy_id,
                "source_package_hash": package.package_hash,
            },
        )
        return StageOutcome(
            artifact_type="package_seeded_hypotheses",
            artifact={
                "schema_version": 1,
                "mode": request.mode.value,
                "source_strategy_id": package.strategy_id,
                "source_package_hash": package.package_hash,
                "source_package": package,
                "seed_parameters": seed_parameters,
                "hypotheses": (hypothesis,),
            },
            reason_codes=(
                "SOURCE_PACKAGE_VERIFIED",
                "UNCHANGED_BASELINE_CONTROL_REQUIRED",
            ),
            subject_id=package.strategy_id,
        )


class PackageSeededArchitectureHandler:
    """Freeze adapt architecture and permit only explicit improve mutations."""

    def __init__(
        self,
        *,
        grammar: Any,
        planner: ArchitecturePlanner,
    ) -> None:
        if grammar is None:
            raise ContractError("grammar is required")
        if not isinstance(planner, ArchitecturePlanner):
            raise ContractError("planner must be an ArchitecturePlanner")
        self._grammar = grammar
        self._planner = planner

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        maximum = 1
        if context.request.mode is FactoryMode.IMPROVE:
            maximum = context.request.improvement_policy.maximum_architectures
        return StageBudgetRequest(architectures=maximum)

    @staticmethod
    def _mutation_types(
        baseline: StrategyArchitecture,
        candidate: StrategyArchitecture,
    ) -> frozenset[str]:
        changes: set[str] = set()
        if baseline.directions != candidate.directions:
            changes.add("directions")
        for name in (
            "context_filters",
            "entry",
            "protection",
            "target",
            "position_management",
            "portfolio_constraints",
        ):
            left = getattr(baseline, name)
            right = getattr(candidate, name)
            if canonical_sha256(left) != canonical_sha256(right):
                changes.add(name)
        for name in ("signal", "setup_adapters", "risk_adapters", "risk"):
            if canonical_sha256(getattr(baseline, name)) != canonical_sha256(
                getattr(candidate, name)
            ):
                changes.add("unsupported_runtime_roles")
        return frozenset(changes)

    @staticmethod
    def _semantic_identity(architecture: StrategyArchitecture) -> str:
        """Hash executable architecture content while ignoring generated identity labels."""

        return canonical_sha256(
            {
                "market_type": architecture.market_type,
                "directions": architecture.directions,
                "signal": architecture.signal,
                "context_filters": architecture.context_filters,
                "setup_adapters": architecture.setup_adapters,
                "entry": architecture.entry,
                "protection": architecture.protection,
                "target": architecture.target,
                "risk_adapters": architecture.risk_adapters,
                "risk": architecture.risk,
                "position_management": architecture.position_management,
                "portfolio_constraints": architecture.portfolio_constraints,
                "parameters": architecture.parameters,
                "grammar_version": architecture.grammar_version,
            }
        )

    @staticmethod
    def _runtime_changes(
        baseline: StrategyArchitecture,
        candidate: StrategyArchitecture,
    ) -> int:
        left = {item.runtime_key for item in baseline.runtime_modules}
        right = {item.runtime_key for item in candidate.runtime_modules}
        return len(left.symmetric_difference(right))

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        seeded = _mapping(
            context.load_artifact(FactoryStage.HYPOTHESIS_GENERATION.value),
            "package-seeded hypotheses",
        )
        package = decode_strategy_package(seeded.get("source_package"))
        baseline = package.architecture
        accepted: tuple[StrategyArchitecture, ...]
        rejected: tuple[Mapping[str, Any], ...]
        reasons: tuple[str, ...]
        if context.request.mode is FactoryMode.ADAPT:
            accepted = (baseline,)
            rejected = ()
            reasons = ("SOURCE_ARCHITECTURE_FROZEN",)
        elif context.request.mode is FactoryMode.IMPROVE:
            policy = context.request.improvement_policy
            accepted_list: list[StrategyArchitecture] = [baseline]
            rejected_list: list[Mapping[str, Any]] = []
            generated = self._grammar.generate(
                dataset=context.request.dataset,
                directions=baseline.directions,
                forbidden_module_ids=context.request.forbidden_module_ids,
                maximum_architectures=policy.maximum_architectures,
                final_candidate=False,
                allowed_signal_module_ids=frozenset({baseline.signal.module_id}),
            )
            seen = {self._semantic_identity(baseline)}
            for item in generated.architectures:
                semantic_identity = self._semantic_identity(item)
                if semantic_identity in seen:
                    continue
                mutation_types = self._mutation_types(baseline, item)
                runtime_changes = self._runtime_changes(baseline, item)
                complexity_increase = max(
                    Decimal("0"),
                    Decimal(len(item.runtime_modules) - len(baseline.runtime_modules)),
                )
                reason_codes: list[str] = []
                if not mutation_types <= policy.allowed_mutations:
                    reason_codes.append("MUTATION_TYPE_FORBIDDEN")
                if runtime_changes > policy.maximum_runtime_module_changes:
                    reason_codes.append("TOO_MANY_RUNTIME_MODULE_CHANGES")
                if complexity_increase > policy.maximum_complexity_increase:
                    reason_codes.append("COMPLEXITY_INCREASE_EXCEEDED")
                if reason_codes:
                    rejected_list.append(
                        {
                            "architecture_id": item.architecture_id,
                            "reason_codes": tuple(sorted(reason_codes)),
                            "mutation_types": tuple(sorted(mutation_types)),
                        }
                    )
                    continue
                accepted_list.append(item)
                seen.add(semantic_identity)
                if len(accepted_list) >= policy.maximum_architectures:
                    break
            for item in generated.rejections:
                rejected_list.append(
                    {
                        "architecture_id": None,
                        "reason_codes": item.reason_codes,
                        "module_keys": item.module_keys,
                    }
                )
            accepted = tuple(accepted_list)
            rejected = tuple(rejected_list)
            reasons = (
                "BASELINE_ARCHITECTURE_RETAINED",
                "STRUCTURAL_MUTATIONS_POLICY_FILTERED",
            )
        else:
            raise CompatibilityError(
                "package-seeded architecture supports only adapt or improve mode"
            )
        if not accepted:
            raise CompatibilityError("package-seeded architecture produced no candidates")
        return StageOutcome(
            artifact_type="package_seeded_architectures",
            artifact={
                "schema_version": 1,
                "mode": context.request.mode.value,
                "source_strategy_id": package.strategy_id,
                "source_package_hash": package.package_hash,
                "baseline_architecture_fingerprint": baseline.fingerprint,
                "architectures": accepted,
                "rejections": rejected,
            },
            reason_codes=reasons,
            subject_id=context.checkpoint.run_id,
        )


_FAMILY_BUDGET_POLICY = "equal-family-capacity-v1"


@dataclass(frozen=True, slots=True)
class _ArchitectureFamilyAllocation:
    family_id: str
    family_capacity: int
    architecture_capacity: int


def _family_balanced_architecture_allocations(
    architectures: tuple[StrategyArchitecture, ...],
    stage_limit: int,
) -> tuple[
    dict[str, _ArchitectureFamilyAllocation],
    tuple[dict[str, int | str], ...],
    int,
]:
    """Allocate equal structural-screening capacity by SIGNAL family, then architecture."""

    if isinstance(stage_limit, bool) or not isinstance(stage_limit, int) or stage_limit <= 0:
        raise ContractError("structural-screening stage limit must be a positive int")
    if not architectures:
        raise CompatibilityError("family budget allocation requires architectures")

    grouped: dict[str, list[StrategyArchitecture]] = {}
    seen_architectures: set[str] = set()
    for architecture in architectures:
        if architecture.architecture_id in seen_architectures:
            raise ContractError("duplicate architecture_id in family budget allocation")
        seen_architectures.add(architecture.architecture_id)
        family_id = architecture.signal.module_id
        if not family_id:
            raise ContractError("strategy family identity requires a SIGNAL module id")
        grouped.setdefault(family_id, []).append(architecture)

    family_capacity = stage_limit // len(grouped)
    if family_capacity < 1:
        raise ContractError(
            "structural-screening budget cannot allocate one experiment to every strategy family"
        )

    allocations: dict[str, _ArchitectureFamilyAllocation] = {}
    family_rows: list[dict[str, int | str]] = []
    allocated_capacity = 0
    for family_id in sorted(grouped):
        family_architectures = sorted(
            grouped[family_id],
            key=lambda item: item.architecture_id,
        )
        architecture_capacity = family_capacity // len(family_architectures)
        if architecture_capacity < 1:
            raise ContractError(
                "structural-screening family budget cannot allocate one experiment "
                f"to every architecture in family {family_id}"
            )
        used_capacity = architecture_capacity * len(family_architectures)
        allocated_capacity += used_capacity
        family_rows.append(
            {
                "family_id": family_id,
                "family_capacity": family_capacity,
                "architecture_count": len(family_architectures),
                "architecture_capacity": architecture_capacity,
                "allocated_capacity": used_capacity,
                "unused_capacity": family_capacity - used_capacity,
            }
        )
        for architecture in family_architectures:
            allocations[architecture.architecture_id] = _ArchitectureFamilyAllocation(
                family_id=family_id,
                family_capacity=family_capacity,
                architecture_capacity=architecture_capacity,
            )

    return allocations, tuple(family_rows), stage_limit - allocated_capacity


@dataclass(frozen=True, slots=True)
class _PlanFamilyAllocation:
    family_id: str
    family_capacity: int
    plan_capacity: int


def _family_balanced_plan_allocations(
    plans: tuple[tuple[str, str, int], ...],
    stage_limit: int,
) -> tuple[
    dict[str, _PlanFamilyAllocation],
    tuple[dict[str, int | str], ...],
    int,
]:
    """Allocate one global promotion-stage budget by family, then by plan."""

    if isinstance(stage_limit, bool) or not isinstance(stage_limit, int) or stage_limit <= 0:
        raise ContractError("promotion stage limit must be a positive int")
    if not plans:
        raise CompatibilityError("family-balanced promotion requires plans")

    grouped: dict[str, list[tuple[str, int]]] = {}
    seen_plan_ids: set[str] = set()
    for plan_id, family_id, available_capacity in plans:
        if not plan_id or not family_id:
            raise ContractError("promotion allocation requires plan and family identity")
        if plan_id in seen_plan_ids:
            raise ContractError("duplicate plan_id in promotion allocation")
        seen_plan_ids.add(plan_id)
        if (
            isinstance(available_capacity, bool)
            or not isinstance(available_capacity, int)
            or available_capacity <= 0
        ):
            raise ContractError("promotion plan capacity must be a positive int")
        grouped.setdefault(family_id, []).append((plan_id, available_capacity))

    family_capacity = stage_limit // len(grouped)
    if family_capacity < 1:
        raise ContractError(
            "promotion budget cannot allocate one candidate to every strategy family"
        )

    allocations: dict[str, _PlanFamilyAllocation] = {}
    rows: list[dict[str, int | str]] = []
    allocated_total = 0
    for family_id in sorted(grouped):
        family_plans = sorted(grouped[family_id], key=lambda item: item[0])
        nominal_plan_capacity = family_capacity // len(family_plans)
        if nominal_plan_capacity < 1:
            raise ContractError(
                "promotion family budget cannot allocate one candidate "
                f"to every plan in family {family_id}"
            )
        used_capacity = 0
        for plan_id, available_capacity in family_plans:
            plan_capacity = min(nominal_plan_capacity, available_capacity)
            allocations[plan_id] = _PlanFamilyAllocation(
                family_id=family_id,
                family_capacity=family_capacity,
                plan_capacity=plan_capacity,
            )
            used_capacity += plan_capacity
        allocated_total += used_capacity
        rows.append(
            {
                "family_id": family_id,
                "family_capacity": family_capacity,
                "plan_count": len(family_plans),
                "nominal_plan_capacity": nominal_plan_capacity,
                "allocated_capacity": used_capacity,
                "unused_capacity": family_capacity - used_capacity,
            }
        )

    return allocations, tuple(rows), stage_limit - allocated_total


class PackageSeededExperimentPlanningHandler:
    """Build parameter plans seeded by the exact source configuration."""

    def __init__(
        self,
        *,
        planner: ArchitecturePlanner,
        research_adapter: ResearchRunnerAdapter,
        policy: ExperimentPlanningPolicy | None = None,
        package_builder: StrategyPackageBuilder | None = None,
    ) -> None:
        if not isinstance(planner, ArchitecturePlanner):
            raise ContractError("planner must be an ArchitecturePlanner")
        if not isinstance(research_adapter, ResearchRunnerAdapter):
            raise ContractError("research_adapter must be a ResearchRunnerAdapter")
        self._planner = planner
        self._research = research_adapter
        self._policy = policy or ExperimentPlanningPolicy()
        self._builder = package_builder or StrategyPackageBuilder()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        artifact = _mapping(
            context.load_artifact(FactoryStage.ARCHITECTURE_GENERATION.value),
            "package-seeded architectures",
        )
        architectures = _sequence(artifact.get("architectures"), "architectures")
        return StageBudgetRequest(architectures=len(architectures))

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        seeded = _mapping(
            context.load_artifact(FactoryStage.HYPOTHESIS_GENERATION.value),
            "package-seeded hypotheses",
        )
        package = decode_strategy_package(seeded.get("source_package"))
        seed_parameters = self._builder.candidate_parameters(package)
        artifact = _mapping(
            context.load_artifact(FactoryStage.ARCHITECTURE_GENERATION.value),
            "package-seeded architectures",
        )
        architectures = tuple(
            decode_strategy_architecture(item)
            for item in _sequence(artifact.get("architectures"), "architectures")
        )
        if not architectures:
            raise CompatibilityError("package-seeded planning requires architectures")
        stage_limit = context.request.budget.stage_experiment_limits.get(
            FactoryStage.STRUCTURAL_SCREENING.value,
            context.request.budget.maximum_experiments,
        )
        allocations, family_allocations, unused_stage_capacity = (
            _family_balanced_architecture_allocations(architectures, stage_limit)
        )
        split = default_research_split(context.request, gap_seconds=self._policy.gap_seconds)
        records: list[dict[str, Any]] = []
        planned_jobs = 0
        baseline_hash = canonical_sha256(seed_parameters)
        for architecture in architectures:
            allocation = allocations[architecture.architecture_id]
            if context.request.mode is FactoryMode.ADAPT:
                adaptation_policy = context.request.adaptation_policy
                allow_search = True
                neighborhood = adaptation_policy.neighborhood_points
                allowed_names = adaptation_policy.allowed_parameter_names
            else:
                improvement_policy = context.request.improvement_policy
                allow_search = "parameters" in improvement_policy.allowed_mutations
                neighborhood = improvement_policy.neighborhood_points
                allowed_names = improvement_policy.allowed_parameter_names
            space = self._planner.build_seeded_parameter_space(
                architecture,
                seed_parameters=seed_parameters,
                maximum_combinations=allocation.architecture_capacity,
                neighborhood_points=neighborhood,
                allowed_parameter_names=allowed_names,
                allow_adaptation=allow_search,
            )
            method = self._policy.search_method
            if method == "staged" and space.maximum_combinations < 3:
                method = "grid"
            preview = self._research.build_plan(
                request=context.request,
                factory_run_id=context.checkpoint.run_id,
                manifest_id=context.manifest.manifest_id,
                stage=FactoryStage.STRUCTURAL_SCREENING,
                architecture=architecture,
                parameter_space=space,
                split=split,
                search_method=method,
                experiment_count=space.maximum_combinations,
            )
            plan = preview.factory_plan
            is_control = architecture.fingerprint == package.architecture.fingerprint
            records.append(
                {
                    "factory_plan": plan,
                    "native_plan_fingerprint": preview.native_plan.fingerprint,
                    "native_specification_fingerprint": preview.native_specification.fingerprint,
                    "split": _split_mapping(split),
                    "search_method": method,
                    "experiment_count": space.maximum_combinations,
                    "strategy_family": allocation.family_id,
                    "family_capacity": allocation.family_capacity,
                    "architecture_capacity": allocation.architecture_capacity,
                    "is_control_arm": is_control,
                    "baseline_configuration_hash": baseline_hash,
                    "source_package_hash": package.package_hash,
                }
            )
            planned_jobs += len(preview.native_plan.jobs)
        control_count = sum(bool(item["is_control_arm"]) for item in records)
        if control_count != 1:
            raise CompatibilityError(
                "package-seeded planning requires exactly one unchanged control arm"
            )
        return StageOutcome(
            artifact_type="package_seeded_experiment_plans",
            artifact={
                "schema_version": 1,
                "mode": context.request.mode.value,
                "source_strategy_id": package.strategy_id,
                "source_package_hash": package.package_hash,
                "baseline_configuration_hash": baseline_hash,
                "control_arm_count": control_count,
                "family_budget_policy": _FAMILY_BUDGET_POLICY,
                "family_allocations": family_allocations,
                "unused_stage_capacity": unused_stage_capacity,
                "plans": tuple(records),
                "planned_job_count": planned_jobs,
            },
            reason_codes=(
                "EXACT_SOURCE_CONTROL_INCLUDED",
                "PACKAGE_SEEDED_PARAMETER_SPACES_FROZEN",
            ),
            subject_id=context.checkpoint.run_id,
        )


@dataclass(frozen=True, slots=True)
class ExperimentPlanningPolicy:
    search_method: str = "staged"
    structural_stage: FactoryStage = FactoryStage.STRUCTURAL_SCREENING
    gap_seconds: int = 0

    def __post_init__(self) -> None:
        if self.search_method not in {"grid", "random", "staged"}:
            raise ContractError("unsupported experiment planning search method")
        if self.structural_stage is not FactoryStage.STRUCTURAL_SCREENING:
            raise ContractError("initial experiment plans must target structural screening")
        if isinstance(self.gap_seconds, bool) or not isinstance(self.gap_seconds, int):
            raise ContractError("gap_seconds must be int")
        if self.gap_seconds < 0:
            raise ContractError("gap_seconds must be non-negative")


class ExperimentPlanningHandler:
    """Build bounded Factory/Research Runner plans without submitting or executing them."""

    def __init__(
        self,
        *,
        planner: ArchitecturePlanner,
        research_adapter: ResearchRunnerAdapter,
        policy: ExperimentPlanningPolicy | None = None,
        split: ResearchSplitPlan | None = None,
    ) -> None:
        self._planner = planner
        self._research = research_adapter
        self._policy = policy or ExperimentPlanningPolicy()
        self._split = split

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.ARCHITECTURE_GENERATION.value),
            "architecture artifact",
        )
        architectures = tuple(
            decode_strategy_architecture(item)
            for item in _sequence(artifact.get("architectures"), "architectures")
        )
        if not architectures:
            raise CompatibilityError("experiment planning requires architectures")
        split = self._split or default_research_split(
            context.request,
            gap_seconds=self._policy.gap_seconds,
        )
        stage_limit = context.request.budget.stage_experiment_limits.get(
            FactoryStage.STRUCTURAL_SCREENING.value,
            context.request.budget.maximum_experiments,
        )
        allocations, family_allocations, unused_stage_capacity = (
            _family_balanced_architecture_allocations(architectures, stage_limit)
        )
        records: list[dict[str, Any]] = []
        total_jobs = 0
        for architecture in architectures:
            allocation = allocations[architecture.architecture_id]
            space = self._planner.build_parameter_space(
                architecture,
                maximum_combinations=allocation.architecture_capacity,
            )
            method = self._policy.search_method
            if method == "staged" and space.maximum_combinations < 3:
                method = "grid"
            prepared = self._research.build_plan(
                request=context.request,
                factory_run_id=context.checkpoint.run_id,
                manifest_id=context.manifest.manifest_id,
                stage=self._policy.structural_stage,
                architecture=architecture,
                parameter_space=space,
                split=split,
                search_method=method,
                experiment_count=space.maximum_combinations,
            )
            job_count = _positive_int(
                prepared.factory_plan.resource_limits.get("job_count"),
                "research job_count",
            )
            total_jobs += job_count
            records.append(
                {
                    "factory_plan": prepared.factory_plan,
                    "split": _split_mapping(split),
                    "search_method": method,
                    "experiment_count": space.maximum_combinations,
                    "strategy_family": allocation.family_id,
                    "family_capacity": allocation.family_capacity,
                    "architecture_capacity": allocation.architecture_capacity,
                }
            )
        return StageOutcome(
            artifact_type="experiment_plans",
            artifact={
                "schema_version": 1,
                "plans": tuple(records),
                "architecture_count": len(architectures),
                "family_budget_policy": _FAMILY_BUDGET_POLICY,
                "family_allocations": family_allocations,
                "unused_stage_capacity": unused_stage_capacity,
                "planned_job_count": total_jobs,
            },
            reason_codes=("EXPERIMENT_PLANS_BUILT", "HOLDOUT_FROZEN_IN_PLAN"),
            subject_id=context.checkpoint.run_id,
        )


@dataclass(frozen=True, slots=True)
class StructuralScreeningPolicy:
    """Fail-closed completion policy for the first executed research batch."""

    accept_partial_results: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.accept_partial_results, bool):
            raise ContractError("accept_partial_results must be boolean")


class StructuralScreeningHandler:
    """Submit, execute, and reconcile frozen structural-screening plans."""

    _SUCCESS = frozenset({"completed", "completed_with_failures"})

    def __init__(
        self,
        *,
        research_adapter: ResearchRunnerAdapter,
        execution_adapter: ResearchRunnerExecutionAdapter,
        policy: StructuralScreeningPolicy | None = None,
    ) -> None:
        self._research = research_adapter
        self._execution = execution_adapter
        self._policy = policy or StructuralScreeningPolicy()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        artifact = _mapping(
            context.load_artifact(FactoryStage.EXPERIMENT_PLANNING.value),
            "experiment planning artifact",
        )
        planned_jobs = _positive_int(
            artifact.get("planned_job_count"),
            "planned_job_count",
        )
        return StageBudgetRequest(experiments=planned_jobs)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.EXPERIMENT_PLANNING.value),
            "experiment planning artifact",
        )
        records = _sequence(artifact.get("plans"), "experiment plans")
        if not records:
            raise CompatibilityError("structural screening requires experiment plans")
        outcomes: list[dict[str, Any]] = []
        partial = False
        for raw_record in records:
            record = _mapping(raw_record, "experiment plan record")
            frozen_plan = decode_experiment_plan(record.get("factory_plan"))
            if frozen_plan.stage is not FactoryStage.STRUCTURAL_SCREENING:
                raise CompatibilityError("structural screening received a plan for another stage")
            split = _split_from_mapping(record.get("split"))
            method = _text(record.get("search_method"), "search_method")
            experiment_count = _positive_int(
                record.get("experiment_count"),
                "experiment_count",
            )
            prepared = self._research.restore_plan(
                request=context.request,
                frozen_plan=frozen_plan,
                split=split,
                search_method=method,
                experiment_count=experiment_count,
            )
            batch_id = f"research-{frozen_plan.plan_id}"
            batch_fingerprint = canonical_sha256(frozen_plan)
            context.register_batch(
                batch_id=batch_id,
                batch_fingerprint=batch_fingerprint,
            )
            submission = self._execution.ensure_submission(prepared)
            result = self._execution.resume(submission.experiment_id)
            if result.failure is not None:
                if result.failure.retryable:
                    raise StageBlockedError(
                        "Research Runner structural screening is retryable: "
                        + result.failure.reason_code
                    )
                raise CompatibilityError(
                    "Research Runner structural screening failed: " + result.failure.reason_code
                )
            snapshot = result.snapshot
            if snapshot is None:
                raise CompatibilityError("Research Runner returned neither a result nor a failure")
            if snapshot.has_nonterminal_runs or snapshot.status not in self._SUCCESS:
                raise StageBlockedError(
                    f"Research Runner structural screening is not terminal: {snapshot.status}"
                )
            if snapshot.status == "completed_with_failures":
                if not self._policy.accept_partial_results:
                    raise CompatibilityError(
                        "partial structural-screening results are forbidden by policy"
                    )
                partial = True
            outcomes.append(
                {
                    "factory_plan_id": frozen_plan.plan_id,
                    "experiment_id": submission.experiment_id,
                    "specification_fingerprint": submission.specification_fingerprint,
                    "plan_fingerprint": submission.plan_fingerprint,
                    "manifest_fingerprint": submission.manifest_fingerprint,
                    "status": snapshot.status,
                    "run_status_counts": snapshot.run_status_counts,
                    "run_ids": snapshot.run_ids,
                }
            )
        reasons = ["STRUCTURAL_SCREENING_COMPLETED"]
        if partial:
            reasons.append("PARTIAL_RESEARCH_RESULTS_RETAINED")
        return StageOutcome(
            artifact_type="structural_screening_results",
            artifact={
                "schema_version": 1,
                "experiments": tuple(outcomes),
                "partial_results": partial,
            },
            reason_codes=tuple(reasons),
            subject_id=context.checkpoint.run_id,
        )


class OptimizationPromotionHandler:
    """Select candidates in Results Aggregator and activate a staged-search phase."""

    _SUCCESS = frozenset({"completed", "completed_with_failures"})

    def __init__(
        self,
        *,
        factory_stage: FactoryStage,
        source_stage: FactoryStage,
        optimization_stage_index: int,
        research_adapter: ResearchRunnerAdapter,
        execution_adapter: ResearchRunnerExecutionAdapter,
        selector: ResultsAggregatorCandidateSelector,
        family_balanced: bool = False,
        freeze_finalists: bool = False,
    ) -> None:
        if factory_stage not in {FactoryStage.COARSE_SEARCH, FactoryStage.LOCAL_REFINEMENT}:
            raise ContractError("optimization promotion supports coarse or local refinement")
        if source_stage not in {
            FactoryStage.STRUCTURAL_SCREENING,
            FactoryStage.COARSE_SEARCH,
        }:
            raise ContractError("optimization promotion source stage is invalid")
        if (
            isinstance(optimization_stage_index, bool)
            or not isinstance(optimization_stage_index, int)
            or optimization_stage_index < 1
        ):
            raise ContractError("optimization_stage_index must be a positive int")
        self._factory_stage = factory_stage
        self._source_stage = source_stage
        self._stage_index = optimization_stage_index
        if not isinstance(family_balanced, bool):
            raise ContractError("family_balanced must be boolean")
        if not isinstance(freeze_finalists, bool):
            raise ContractError("freeze_finalists must be boolean")
        if freeze_finalists and factory_stage is not FactoryStage.LOCAL_REFINEMENT:
            raise ContractError("finalists may be frozen only after local refinement")
        if freeze_finalists and not family_balanced:
            raise ContractError("frozen finalist selection requires family-balanced SEARCH")
        self._research = research_adapter
        self._execution = execution_adapter
        self._selector = selector
        self._family_balanced = family_balanced
        self._freeze_finalists = freeze_finalists

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        planning = _mapping(
            context.load_artifact(FactoryStage.EXPERIMENT_PLANNING.value),
            "experiment planning artifact",
        )
        plans = _sequence(planning.get("plans"), "experiment plans")
        plan_records = tuple(_mapping(item, "experiment plan record") for item in plans)
        stage_limit = context.request.budget.stage_experiment_limits.get(
            self._factory_stage.value,
            context.request.budget.maximum_experiments,
        )
        if self._family_balanced:
            _, _, unused = _family_balanced_plan_allocations(
                tuple(
                    (
                        decode_experiment_plan(record.get("factory_plan")).plan_id,
                        _text(record.get("strategy_family"), "strategy_family"),
                        decode_experiment_plan(
                            record.get("factory_plan")
                        ).parameter_space.maximum_combinations,
                    )
                    for record in plan_records
                ),
                stage_limit,
            )
            return StageBudgetRequest(experiments=stage_limit - unused)
        capacity = sum(
            decode_experiment_plan(record.get("factory_plan")).parameter_space.maximum_combinations
            for record in plan_records
        )
        return StageBudgetRequest(experiments=min(stage_limit, capacity))

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        baseline_select: Callable[..., Any] | None = None
        if context.request.mode in {FactoryMode.ADAPT, FactoryMode.IMPROVE}:
            candidate_select = getattr(self._selector, "select_with_baseline", None)
            if not callable(candidate_select):
                raise CompatibilityError(
                    "adapt/improve promotion requires a baseline-aware selector"
                )
            baseline_select = candidate_select
        planning = _mapping(
            context.load_artifact(FactoryStage.EXPERIMENT_PLANNING.value),
            "experiment planning artifact",
        )
        source = _mapping(
            context.load_artifact(self._source_stage.value),
            f"{self._source_stage.value} artifact",
        )
        source_experiments = {
            _text(item.get("factory_plan_id"), "factory_plan_id"): _mapping(
                item, "source experiment"
            )
            for item in (
                _mapping(raw, "source experiment")
                for raw in _sequence(source.get("experiments"), "source experiments")
            )
        }
        plan_records = tuple(
            _mapping(raw, "experiment plan record")
            for raw in _sequence(planning.get("plans"), "experiment plans")
        )
        baseline_record: Mapping[str, Any] | None = None
        baseline_plan: ExperimentPlan | None = None
        baseline_experiment_id: str | None = None
        baseline_configuration_hash: str | None = None
        if context.request.mode in {FactoryMode.ADAPT, FactoryMode.IMPROVE}:
            controls = tuple(
                record for record in plan_records if record.get("is_control_arm") is True
            )
            if len(controls) != 1:
                raise CompatibilityError(
                    "adapt/improve promotion requires exactly one control-arm plan"
                )
            baseline_record = controls[0]
            baseline_plan = decode_experiment_plan(baseline_record.get("factory_plan"))
            source_control = source_experiments.get(baseline_plan.plan_id)
            if source_control is None:
                raise CompatibilityError(
                    "source stage has no experiment for the baseline control arm"
                )
            baseline_experiment_id = _text(
                source_control.get("experiment_id"), "baseline experiment_id"
            )
            baseline_configuration_hash = _text(
                baseline_record.get("baseline_configuration_hash"),
                "baseline_configuration_hash",
            )
        outputs: list[dict[str, Any]] = []
        stage_limit = context.request.budget.stage_experiment_limits.get(
            self._factory_stage.value,
            context.request.budget.maximum_experiments,
        )
        promotion_allocations: dict[str, _PlanFamilyAllocation] = {}
        promotion_family_rows: tuple[dict[str, int | str], ...] = ()
        promotion_unused_capacity = 0
        if self._family_balanced:
            promotion_allocations, promotion_family_rows, promotion_unused_capacity = (
                _family_balanced_plan_allocations(
                    tuple(
                        (
                            decode_experiment_plan(record.get("factory_plan")).plan_id,
                            _text(record.get("strategy_family"), "strategy_family"),
                            decode_experiment_plan(
                                record.get("factory_plan")
                            ).parameter_space.maximum_combinations,
                        )
                        for record in plan_records
                    ),
                    stage_limit,
                )
            )
        for record in plan_records:
            plan = decode_experiment_plan(record.get("factory_plan"))
            source_experiment = source_experiments.get(plan.plan_id)
            if source_experiment is None:
                raise CompatibilityError(
                    f"source stage has no experiment for Factory plan {plan.plan_id}"
                )
            experiment_id = _text(source_experiment.get("experiment_id"), "experiment_id")
            split = _split_from_mapping(record.get("split"))
            method = _text(record.get("search_method"), "search_method")
            experiment_count = _positive_int(record.get("experiment_count"), "experiment_count")
            prepared = self._research.restore_plan(
                request=context.request,
                frozen_plan=plan,
                split=split,
                search_method=method,
                experiment_count=experiment_count,
            )
            restored_submission = self._execution.restore_submission(
                prepared,
                expected_experiment_id=experiment_id,
            )
            if restored_submission.experiment_id != experiment_id:
                raise CompatibilityError("restored Research Runner submission identity changed")
            if self._family_balanced:
                allocation = promotion_allocations.get(plan.plan_id)
                if allocation is None:
                    raise CompatibilityError(
                        "family-balanced promotion has no allocation for Factory plan"
                    )
                strategy_family = _text(record.get("strategy_family"), "strategy_family")
                if (
                    allocation.family_id != strategy_family
                    or plan.architecture.signal.module_id != strategy_family
                ):
                    raise CompatibilityError("family-balanced promotion family identity drift")
                maximum_candidates = allocation.plan_capacity
            else:
                allocation = None
                maximum_candidates = min(stage_limit, plan.parameter_space.maximum_combinations)
            if context.request.mode in {FactoryMode.ADAPT, FactoryMode.IMPROVE}:
                assert baseline_plan is not None
                assert baseline_experiment_id is not None
                assert baseline_configuration_hash is not None
                include_control = experiment_id == baseline_experiment_id
                maximum_complexity_increase = (
                    Decimal("0")
                    if context.request.mode is FactoryMode.ADAPT
                    else context.request.improvement_policy.maximum_complexity_increase
                )
                if baseline_select is None:
                    raise CompatibilityError(
                        "adapt/improve promotion requires a baseline-aware selector"
                    )
                selection = baseline_select(
                    experiment_id=experiment_id,
                    architecture=plan.architecture,
                    maximum_candidates=maximum_candidates,
                    generated_at=context.checkpoint.updated_at,
                    baseline_configuration_hash=baseline_configuration_hash,
                    mode=context.request.mode,
                    maximum_complexity_increase=maximum_complexity_increase,
                    baseline_experiment_id=baseline_experiment_id,
                    baseline_architecture=baseline_plan.architecture,
                    include_baseline_control=include_control,
                )
            else:
                if self._family_balanced:
                    selection = self._selector.select(
                        experiment_id=experiment_id,
                        architecture=plan.architecture,
                        maximum_candidates=maximum_candidates,
                        generated_at=context.checkpoint.updated_at,
                        allow_empty=True,
                    )
                else:
                    selection = self._selector.select(
                        experiment_id=experiment_id,
                        architecture=plan.architecture,
                        maximum_candidates=maximum_candidates,
                        generated_at=context.checkpoint.updated_at,
                    )
            frozen = set(self._execution.frozen_configuration_hashes(experiment_id))
            selected = selection.activation_hashes
            if not set(selected) <= frozen:
                raise CompatibilityError(
                    "Results Aggregator selected a configuration outside the frozen plan"
                )
            optimization_stages = getattr(prepared.native_plan, "optimization_stages", ())
            available_indices = {getattr(item, "stage_index", None) for item in optimization_stages}
            run_ids: tuple[str, ...]
            if not selection.selected:
                run_ids = ()
                status = "no_eligible_candidates"
            elif self._stage_index in available_indices:
                activation = self._execution.activate_optimization_stage(
                    experiment_id,
                    stage_index=self._stage_index,
                    candidate_configuration_hashes=selected,
                    activated_at=context.checkpoint.updated_at,
                )
                result = self._execution.resume(experiment_id)
                if result.failure is not None:
                    if result.failure.retryable:
                        raise StageBlockedError(result.failure.reason_code)
                    raise CompatibilityError(result.failure.reason_code)
                snapshot = result.snapshot
                if (
                    snapshot is None
                    or snapshot.has_nonterminal_runs
                    or snapshot.status not in self._SUCCESS
                ):
                    raise StageBlockedError("promoted Research Runner stage is not terminal")
                run_ids = activation.run_ids
                status = snapshot.status
            else:
                run_ids = ()
                status = "not_applicable"
            baseline_control = getattr(selection, "baseline_control", None)
            output = {
                "factory_plan_id": plan.plan_id,
                "experiment_id": experiment_id,
                "optimization_stage_index": self._stage_index,
                "selected_configuration_hashes": selected,
                "selected_candidate_ids": tuple(
                    item.candidate.candidate_id for item in selection.selected
                ),
                "baseline_control_candidate_id": (
                    baseline_control.candidate.candidate_id
                    if baseline_control is not None
                    else None
                ),
                "baseline_experiment_id": baseline_experiment_id,
                "rejected_candidates": tuple(
                    {
                        "candidate_id": item.candidate.candidate_id,
                        "reason_codes": item.selection_reasons,
                    }
                    for item in selection.rejected
                ),
                "run_ids": run_ids,
                "status": status,
            }
            if allocation is not None:
                output.update(
                    {
                        "strategy_family": allocation.family_id,
                        "family_capacity": allocation.family_capacity,
                        "plan_capacity": allocation.plan_capacity,
                    }
                )
            outputs.append(output)
        promotion_artifact: dict[str, Any] = {
            "schema_version": 1,
            "experiments": tuple(outputs),
        }
        promotion_reasons = [
            "CANDIDATES_SELECTED_FROM_RESULTS_AGGREGATOR",
            "OPTIMIZATION_STAGE_RECONCILED",
        ]
        finalists_selected: int | None = None
        if self._family_balanced:
            promotion_artifact.update(
                {
                    "family_budget_policy": _FAMILY_BUDGET_POLICY,
                    "family_allocations": promotion_family_rows,
                    "unused_stage_capacity": promotion_unused_capacity,
                }
            )
            promotion_reasons.append("FAMILY_BALANCED_PROMOTION_CAPACITY")
        if self._freeze_finalists:
            if context.request.mode is not FactoryMode.CREATE:
                raise CompatibilityError(
                    "frozen cross-family finalists are supported only for CREATE SEARCH"
                )
            plan_by_id = {
                decode_experiment_plan(record.get("factory_plan")).plan_id: (
                    decode_experiment_plan(record.get("factory_plan")),
                    _text(record.get("strategy_family"), "strategy_family"),
                )
                for record in plan_records
            }
            experiment_sources: list[tuple[str, StrategyArchitecture]] = []
            experiment_metadata: dict[str, tuple[str, str]] = {}
            for output in outputs:
                plan_id = _text(output.get("factory_plan_id"), "factory_plan_id")
                experiment_id = _text(output.get("experiment_id"), "experiment_id")
                source_plan = plan_by_id.get(plan_id)
                if source_plan is None:
                    raise CompatibilityError("finalist selection references an unknown plan")
                plan, strategy_family = source_plan
                if plan.architecture.signal.module_id != strategy_family:
                    raise CompatibilityError("finalist source family identity drift")
                if experiment_id in experiment_metadata:
                    raise CompatibilityError("finalist selection has duplicate experiment identity")
                experiment_sources.append((experiment_id, plan.architecture))
                experiment_metadata[experiment_id] = (plan_id, strategy_family)

            allowed_producer_hashes: dict[str, frozenset[str]] = {}
            for output in outputs:
                experiment_id = _text(output.get("experiment_id"), "experiment_id")
                raw_hashes = _sequence(
                    output.get("selected_configuration_hashes", ()),
                    "selected_configuration_hashes",
                )
                hashes = tuple(_text(item, "selected_configuration_hash") for item in raw_hashes)
                if len(hashes) != len(set(hashes)):
                    raise CompatibilityError(
                        "LOCAL_REFINEMENT selected duplicate producer configurations"
                    )
                allowed_producer_hashes[experiment_id] = frozenset(hashes)
            selection = self._selector.select_across_experiments(
                experiments=tuple(experiment_sources),
                allowed_producer_hashes=allowed_producer_hashes,
                maximum_candidates=context.request.budget.maximum_finalists,
                generated_at=context.checkpoint.updated_at,
            )
            finalist_rows: list[dict[str, Any]] = []
            for ranked in selection.selected:
                candidate = ranked.candidate
                experiment_id = selection.source_experiment_ids[candidate.candidate_id]
                plan_id, strategy_family = experiment_metadata[experiment_id]
                if candidate.architecture.signal.module_id != strategy_family:
                    raise CompatibilityError("global finalist family identity drift")
                finalist_rows.append(
                    {
                        "candidate": candidate,
                        "source_experiment_id": experiment_id,
                        "source_factory_plan_id": plan_id,
                        "strategy_family": strategy_family,
                        "producer_configuration_hash": (
                            selection.producer_configuration_hashes[candidate.candidate_id]
                        ),
                        "pareto_front": ranked.pareto_front,
                        "constraint_results": ranked.constraint_results,
                        "objective_values": ranked.objective_values,
                        "selection_reasons": ranked.selection_reasons,
                    }
                )
            selection_fingerprint = canonical_sha256(
                {
                    "policy": "global-pareto-finalists-v1",
                    "run_id": context.checkpoint.run_id,
                    "finalists": tuple(
                        (
                            row["candidate"].candidate_id,
                            row["candidate"].configuration_hash,
                            row["source_experiment_id"],
                            row["source_factory_plan_id"],
                            row["strategy_family"],
                        )
                        for row in finalist_rows
                    ),
                    "source_experiments": tuple(sorted(experiment_metadata)),
                }
            )
            context.register_batch(
                batch_id=f"search-finalists-{context.checkpoint.run_id}",
                batch_fingerprint=selection_fingerprint,
            )
            promotion_artifact.update(
                {
                    "finalist_selection_policy": "global-pareto-finalists-v1",
                    "finalist_selection_fingerprint": selection_fingerprint,
                    "finalist_count": len(finalist_rows),
                    "finalists": tuple(finalist_rows),
                    "rejected_finalists": tuple(
                        {
                            "candidate_id": item.candidate.candidate_id,
                            "reason_codes": item.selection_reasons,
                        }
                        for item in selection.rejected
                    ),
                }
            )
            finalists_selected = len(finalist_rows)
            promotion_reasons.append(
                "GLOBAL_SEARCH_FINALISTS_FROZEN"
                if finalist_rows
                else "NO_ADMISSIBLE_SEARCH_FINALISTS"
            )
        return StageOutcome(
            artifact_type=f"{self._factory_stage.value}_results",
            artifact=promotion_artifact,
            reason_codes=tuple(promotion_reasons),
            subject_id=context.checkpoint.run_id,
            finalists_selected=finalists_selected,
        )


class BaselinePackageValidationHandler:
    """Revalidate one immutable Strategy Package without architecture or parameter search."""

    _SUCCESS = frozenset({"completed", "completed_with_failures"})

    def __init__(
        self,
        *,
        package_source: StrategyPackageSourcePort,
        research_adapter: ResearchRunnerAdapter,
        execution_adapter: ResearchRunnerExecutionAdapter,
        package_builder: StrategyPackageBuilder | None = None,
        gap_seconds: int = 0,
    ) -> None:
        if not isinstance(package_source, StrategyPackageSourcePort):
            raise TypeError("package_source must implement StrategyPackageSourcePort")
        if isinstance(gap_seconds, bool) or not isinstance(gap_seconds, int):
            raise TypeError("gap_seconds must be int")
        if gap_seconds < 0:
            raise ValueError("gap_seconds must be non-negative")
        self._source = package_source
        self._research = research_adapter
        self._execution = execution_adapter
        self._package_builder = package_builder or StrategyPackageBuilder()
        self._gap_seconds = gap_seconds

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        limit = context.request.budget.stage_experiment_limits.get(FactoryStage.WALK_FORWARD.value)
        if limit is None:
            raise CompatibilityError("baseline validation requires an explicit walk-forward budget")
        return StageBudgetRequest(experiments=limit)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        if context.request.mode is not FactoryMode.VALIDATE:
            raise CompatibilityError("baseline package validation is valid only in validate mode")
        strategy_id = context.request.baseline_strategy_id
        if strategy_id is None:
            raise CompatibilityError("validate mode has no baseline strategy ID")
        loaded = self._source.load(strategy_id)
        package = loaded if isinstance(loaded, StrategyPackage) else decode_strategy_package(loaded)
        self._package_builder.verify(package)
        self._validate_scope(context.request, package, strategy_id)
        parameters = self._package_builder.candidate_parameters(package)
        candidate = CandidateStrategy(
            candidate_id=(
                f"validate-{canonical_sha256((strategy_id, context.request.dataset.fingerprint))[:24]}"
            ),
            architecture=package.architecture,
            parameters=parameters,
            configuration_hash=canonical_sha256(parameters),
            lineage_references=(
                f"strategy-package:{package.strategy_id}",
                f"strategy-package-hash:{package.package_hash}",
            ),
            evaluation=None,
        )
        split = default_research_split(context.request, gap_seconds=self._gap_seconds)
        prepared = self._research.build_walk_forward_plan(
            request=context.request,
            factory_run_id=context.checkpoint.run_id,
            manifest_id=context.manifest.manifest_id,
            candidate=candidate,
            split=split,
        )
        planned_jobs = _positive_int(
            prepared.factory_plan.resource_limits.get("planned_total_jobs"),
            "walk-forward planned_total_jobs",
        )
        stage_limit = context.request.budget.stage_experiment_limits[
            FactoryStage.WALK_FORWARD.value
        ]
        if planned_jobs > stage_limit:
            raise CompatibilityError(
                "baseline validation walk-forward plan exceeds its stage budget"
            )
        context.register_batch(
            batch_id=f"baseline-validation-{context.checkpoint.run_id}",
            batch_fingerprint=canonical_sha256(
                (package.package_hash, candidate.configuration_hash, split)
            ),
        )
        submission = self._execution.ensure_submission(prepared)
        train_snapshot = _terminal_research_snapshot(
            self._execution.resume(submission.experiment_id),
            operation="baseline-validation train",
            success=self._SUCCESS,
        )
        native_hashes = self._execution.frozen_configuration_hashes(submission.experiment_id)
        if len(native_hashes) != 1:
            raise CompatibilityError("baseline validation froze more than one configuration")
        native_hash = native_hashes[0]
        validation = self._execution.activate_stage(
            submission.experiment_id,
            role="validation",
            candidate_configuration_hashes=(native_hash,),
            activated_at=context.checkpoint.updated_at,
        )
        validation_snapshot = _terminal_research_snapshot(
            self._execution.resume(submission.experiment_id),
            operation="baseline-validation validation",
            success=self._SUCCESS,
        )
        oos = self._execution.activate_stage(
            submission.experiment_id,
            role="oos",
            candidate_configuration_hashes=(native_hash,),
            activated_at=context.checkpoint.updated_at,
        )
        oos_snapshot = _terminal_research_snapshot(
            self._execution.resume(submission.experiment_id),
            operation="baseline-validation OOS",
            success=self._SUCCESS,
        )
        windows = {
            getattr(stage, "window_id", None)
            for stage in prepared.native_plan.stages
            if getattr(getattr(stage, "role", None), "value", None) == "oos"
        }
        windows.discard(None)
        return StageOutcome(
            artifact_type="walk_forward_results",
            artifact={
                "schema_version": 1,
                "mode": FactoryMode.VALIDATE.value,
                "source_strategy_id": package.strategy_id,
                "source_package_hash": package.package_hash,
                "candidates": (
                    {
                        "candidate": candidate,
                        "source_selection_reasons": (
                            "BASELINE_PACKAGE_ARCHITECTURE_FROZEN",
                            "BASELINE_PACKAGE_PARAMETERS_FROZEN",
                            "NO_REOPTIMIZATION_PERFORMED",
                        ),
                        "factory_plan": prepared.factory_plan,
                        "experiment_id": submission.experiment_id,
                        "native_configuration_hash": native_hash,
                        "train_run_ids": train_snapshot.run_ids,
                        "validation_run_ids": validation.run_ids,
                        "oos_run_ids": oos.run_ids,
                        "validation_status": validation_snapshot.status,
                        "oos_status": oos_snapshot.status,
                        "walk_forward_window_count": len(windows),
                        "planned_job_count": planned_jobs,
                        "split": split,
                    },
                ),
                "budget_rejected_candidate_ids": (),
                "consumed_job_budget": planned_jobs,
            },
            reason_codes=(
                "BASELINE_PACKAGE_REVALIDATED_WITHOUT_REOPTIMIZATION",
                "WALK_FORWARD_TRAIN_VALIDATION_OOS_COMPLETED",
                "FINAL_HOLDOUT_REMAINS_UNTOUCHED",
            ),
            subject_id=package.strategy_id,
            finalists_selected=1,
        )

    @staticmethod
    def _validate_scope(
        request: StrategyFactoryRequest,
        package: StrategyPackage,
        requested_strategy_id: str,
    ) -> None:
        if package.strategy_id != requested_strategy_id:
            raise CompatibilityError("Strategy Package source returned another strategy identity")
        if package.symbol != request.symbol:
            raise CompatibilityError("baseline Strategy Package symbol mismatch")
        if package.exchange != request.exchange:
            raise CompatibilityError("baseline Strategy Package exchange mismatch")
        if package.market_type is not request.market_type:
            raise CompatibilityError("baseline Strategy Package market type mismatch")
        if package.architecture.market_type is not request.market_type:
            raise CompatibilityError("baseline architecture market type mismatch")
        missing_timeframes = sorted(set(package.timeframes) - set(request.allowed_timeframes))
        if missing_timeframes:
            raise CompatibilityError(
                "validate request omits baseline timeframes: " + ", ".join(missing_timeframes)
            )
        if package.live_trading_authorized:
            raise CompatibilityError("baseline Strategy Package authorizes live trading")
        if package.admission_status is AdmissionStatus.REJECTED:
            raise CompatibilityError("a rejected Strategy Package cannot enter validate mode")


class WalkForwardValidationHandler:
    """Run frozen finalists through a separate single-candidate walk-forward experiment."""

    _SUCCESS = frozenset({"completed", "completed_with_failures"})

    def __init__(
        self,
        *,
        research_adapter: ResearchRunnerAdapter,
        execution_adapter: ResearchRunnerExecutionAdapter,
        selector: ResultsAggregatorCandidateSelector,
    ) -> None:
        self._research = research_adapter
        self._execution = execution_adapter
        self._selector = selector

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        limit = context.request.budget.stage_experiment_limits.get(FactoryStage.WALK_FORWARD.value)
        if limit is None:
            raise CompatibilityError(
                "walk-forward requires an explicit stage_experiment_limits budget"
            )
        return StageBudgetRequest(experiments=limit)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        planning = _mapping(
            context.load_artifact(FactoryStage.EXPERIMENT_PLANNING.value),
            "experiment planning artifact",
        )
        source = _mapping(
            context.load_artifact(FactoryStage.LOCAL_REFINEMENT.value),
            "local refinement artifact",
        )
        source_experiments = {
            _text(item.get("factory_plan_id"), "factory_plan_id"): _mapping(
                item, "local refinement experiment"
            )
            for item in (
                _mapping(raw, "local refinement experiment")
                for raw in _sequence(source.get("experiments"), "source experiments")
            )
        }
        split_by_plan: dict[str, ResearchSplitPlan] = {}
        plan_by_id: dict[str, ExperimentPlan] = {}
        for raw_record in _sequence(planning.get("plans"), "experiment plans"):
            record = _mapping(raw_record, "experiment plan record")
            plan = decode_experiment_plan(record.get("factory_plan"))
            plan_by_id[plan.plan_id] = plan
            split_by_plan[plan.plan_id] = _split_from_mapping(record.get("split"))

        maximum_finalists = context.request.budget.maximum_finalists
        stage_limit = context.request.budget.stage_experiment_limits[
            FactoryStage.WALK_FORWARD.value
        ]
        candidate_records: list[tuple[Any, Any, ResearchSplitPlan]] = []
        frozen_finalists = source.get("finalists")
        if frozen_finalists is not None:
            records = _sequence(frozen_finalists, "frozen search finalists")
            if not records:
                raise CompatibilityError("SEARCH produced no frozen finalist for walk-forward")
            if len(records) > maximum_finalists:
                raise CompatibilityError("frozen finalist set exceeds maximum_finalists")
            for raw in records:
                record = _mapping(raw, "frozen search finalist")
                candidate = decode_candidate_strategy(record.get("candidate"))
                plan_id = _text(record.get("source_factory_plan_id"), "source_factory_plan_id")
                experiment_id = _text(
                    record.get("source_experiment_id"),
                    "source_experiment_id",
                )
                strategy_family = _text(record.get("strategy_family"), "strategy_family")
                selected_plan = plan_by_id.get(plan_id)
                if selected_plan is None:
                    raise CompatibilityError("frozen finalist references unknown Factory plan")
                source_experiment = source_experiments.get(plan_id)
                if (
                    source_experiment is None
                    or _text(
                        source_experiment.get("experiment_id"),
                        "experiment_id",
                    )
                    != experiment_id
                ):
                    raise CompatibilityError("frozen finalist experiment lineage drifted")
                if (
                    selected_plan.architecture.fingerprint != candidate.architecture.fingerprint
                    or candidate.architecture.signal.module_id != strategy_family
                ):
                    raise CompatibilityError("frozen finalist architecture/family lineage drifted")
                pareto_front = _positive_int(record.get("pareto_front"), "pareto_front")
                selection_reasons = tuple(
                    _text(item, "selection_reason")
                    for item in _sequence(
                        record.get("selection_reasons"),
                        "selection_reasons",
                    )
                )
                ranked = RankedCandidate(
                    candidate=candidate,
                    pareto_front=pareto_front,
                    constraint_results={},
                    objective_values={},
                    selection_reasons=selection_reasons,
                )
                candidate_records.append((ranked, selected_plan, split_by_plan[plan_id]))
        else:
            remaining = maximum_finalists
            for plan_id in sorted(source_experiments):
                if remaining <= 0:
                    break
                selected_plan = plan_by_id.get(plan_id)
                if selected_plan is None:
                    raise CompatibilityError(
                        f"local refinement references unknown Factory plan {plan_id}"
                    )
                source_experiment = source_experiments[plan_id]
                experiment_id = _text(source_experiment.get("experiment_id"), "experiment_id")
                selection = self._selector.select(
                    experiment_id=experiment_id,
                    architecture=selected_plan.architecture,
                    maximum_candidates=remaining,
                    generated_at=context.checkpoint.updated_at,
                )
                for ranked in selection.selected:
                    candidate_records.append((ranked, selected_plan, split_by_plan[plan_id]))
                    remaining -= 1
                    if remaining <= 0:
                        break
        if not candidate_records:
            raise CompatibilityError("Results Aggregator selected no finalists for walk-forward")
        selection_fingerprint = canonical_sha256(
            tuple(
                (
                    ranked.candidate.candidate_id,
                    ranked.candidate.configuration_hash,
                    plan.plan_id,
                )
                for ranked, plan, _ in candidate_records
            )
        )
        context.register_batch(
            batch_id=f"walk-forward-selection-{context.checkpoint.run_id}",
            batch_fingerprint=selection_fingerprint,
        )

        outputs: list[dict[str, Any]] = []
        consumed_jobs = 0
        budget_rejected: list[str] = []
        for ranked, _, split in candidate_records:
            candidate = ranked.candidate
            prepared = self._research.build_walk_forward_plan(
                request=context.request,
                factory_run_id=context.checkpoint.run_id,
                manifest_id=context.manifest.manifest_id,
                candidate=candidate,
                split=split,
            )
            planned_jobs = _positive_int(
                prepared.factory_plan.resource_limits.get("planned_total_jobs"),
                "walk-forward planned_total_jobs",
            )
            if consumed_jobs + planned_jobs > stage_limit:
                budget_rejected.append(candidate.candidate_id)
                continue
            consumed_jobs += planned_jobs
            submission = self._execution.ensure_submission(prepared)
            initial = self._execution.resume(submission.experiment_id)
            initial_snapshot = _terminal_research_snapshot(
                initial,
                operation="walk-forward train",
                success=self._SUCCESS,
            )
            native_hashes = self._execution.frozen_configuration_hashes(submission.experiment_id)
            if len(native_hashes) != 1:
                raise CompatibilityError(
                    "single-candidate walk-forward experiment froze multiple configurations"
                )
            native_hash = native_hashes[0]
            validation = self._execution.activate_stage(
                submission.experiment_id,
                role="validation",
                candidate_configuration_hashes=(native_hash,),
                activated_at=context.checkpoint.updated_at,
            )
            validation_snapshot = _terminal_research_snapshot(
                self._execution.resume(submission.experiment_id),
                operation="walk-forward validation",
                success=self._SUCCESS,
            )
            oos = self._execution.activate_stage(
                submission.experiment_id,
                role="oos",
                candidate_configuration_hashes=(native_hash,),
                activated_at=context.checkpoint.updated_at,
            )
            oos_snapshot = _terminal_research_snapshot(
                self._execution.resume(submission.experiment_id),
                operation="walk-forward OOS",
                success=self._SUCCESS,
            )
            oos_windows = {
                getattr(stage, "window_id", None)
                for stage in prepared.native_plan.stages
                if getattr(getattr(stage, "role", None), "value", None) == "oos"
            }
            oos_windows.discard(None)
            outputs.append(
                {
                    "candidate": candidate,
                    "source_selection_reasons": ranked.selection_reasons,
                    "factory_plan": prepared.factory_plan,
                    "experiment_id": submission.experiment_id,
                    "native_configuration_hash": native_hash,
                    "train_run_ids": initial_snapshot.run_ids,
                    "validation_run_ids": validation.run_ids,
                    "oos_run_ids": oos.run_ids,
                    "validation_status": validation_snapshot.status,
                    "oos_status": oos_snapshot.status,
                    "walk_forward_window_count": len(oos_windows),
                    "planned_job_count": planned_jobs,
                    "split": split,
                }
            )
        if not outputs:
            raise CompatibilityError("walk-forward budget cannot execute any selected finalist")
        reasons = [
            "WALK_FORWARD_TRAIN_VALIDATION_OOS_COMPLETED",
            "FINAL_HOLDOUT_REMAINS_UNTOUCHED",
        ]
        if budget_rejected:
            reasons.append("WALK_FORWARD_BUDGET_PRUNED")
        return StageOutcome(
            artifact_type="walk_forward_results",
            artifact={
                "schema_version": 1,
                "candidates": tuple(outputs),
                "budget_rejected_candidate_ids": tuple(sorted(budget_rejected)),
                "consumed_job_budget": consumed_jobs,
            },
            reason_codes=tuple(reasons),
            subject_id=context.checkpoint.run_id,
            finalists_selected=len(outputs),
        )


class RobustnessEvidenceHandler:
    """Gate walk-forward finalists using canonical Results Aggregator evidence."""

    def __init__(
        self,
        *,
        aggregator: ResultsAggregatorAdapter,
        gate: CandidateGate,
    ) -> None:
        self._aggregator = aggregator
        self._gate = gate

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.WALK_FORWARD.value),
            "walk-forward artifact",
        )
        records = _sequence(artifact.get("candidates"), "walk-forward candidates")
        if not records:
            raise CompatibilityError("robustness gate requires walk-forward candidates")
        outputs: list[dict[str, Any]] = []
        passed_count = 0
        for raw_record in records:
            record = _mapping(raw_record, "walk-forward candidate")
            frozen = decode_candidate_strategy(record.get("candidate"))
            experiment_id = _text(record.get("experiment_id"), "experiment_id")
            population = self._aggregator.candidates_for_experiment(
                experiment_id,
                generated_at=context.checkpoint.updated_at,
            )
            matching = tuple(
                item for item in population if item.configuration_hash == frozen.configuration_hash
            )
            if len(matching) != 1:
                raise CompatibilityError(
                    "Results Aggregator must return exactly one walk-forward "
                    f"evaluation for {frozen.candidate_id}"
                )
            aggregated = matching[0]
            refreshed = replace(
                frozen,
                lineage_references=tuple(
                    sorted(
                        set(frozen.lineage_references)
                        | set(aggregated.run_ids)
                        | {
                            f"results-aggregator:{experiment_id}",
                            f"walk-forward:{experiment_id}",
                        }
                    )
                ),
                evaluation=replace(
                    aggregated.evaluation,
                    candidate_id=frozen.candidate_id,
                ),
            )
            gate_result = self._gate.evaluate(refreshed)
            refreshed_evaluation = refreshed.evaluation
            if refreshed_evaluation is None:
                raise CompatibilityError("walk-forward candidate lacks refreshed evaluation")
            report = RobustnessReport(
                report_id=(
                    "robustness-"
                    + canonical_sha256(
                        (
                            context.checkpoint.run_id,
                            refreshed.candidate_id,
                            gate_result.policy_fingerprint,
                            aggregated.evaluation.evaluation_id,
                        )
                    )[:24]
                ),
                candidate_id=refreshed.candidate_id,
                passed=gate_result.eligible,
                scenario_results={
                    "checks": gate_result.checks,
                    "evidence": gate_result.evidence,
                    "policy_fingerprint": gate_result.policy_fingerprint,
                    "results_aggregator_evaluation_id": (aggregated.evaluation.evaluation_id),
                    "instability_flags": refreshed_evaluation.instability_flags,
                },
                failure_reasons=gate_result.reason_codes,
                generated_at=context.checkpoint.updated_at,
            )
            if report.passed:
                passed_count += 1
            outputs.append(
                {
                    "candidate": refreshed,
                    "robustness_report": report,
                    "source_walk_forward_experiment_id": experiment_id,
                }
            )
        reasons = ["ROBUSTNESS_EVIDENCE_EVALUATED"]
        reasons.append(
            "ROBUSTNESS_FINALISTS_AVAILABLE" if passed_count else "NO_CANDIDATE_PASSED_ROBUSTNESS"
        )
        return StageOutcome(
            artifact_type="robustness_results",
            artifact={
                "schema_version": 1,
                "candidates": tuple(outputs),
                "passed_candidate_count": passed_count,
            },
            reason_codes=tuple(reasons),
            subject_id=context.checkpoint.run_id,
            finalists_selected=passed_count,
        )


@dataclass(frozen=True, slots=True)
class HoldoutEvaluationPolicy:
    minimum_profit_factor: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if (
            not isinstance(self.minimum_profit_factor, Decimal)
            or not self.minimum_profit_factor.is_finite()
            or self.minimum_profit_factor < 0
        ):
            raise ContractError("minimum_profit_factor must be a finite non-negative Decimal")


class IndependentHoldoutHandler:
    """Open final untouched data once through the native FinalTestGrant boundary."""

    _SUCCESS = frozenset({"completed", "completed_with_failures"})

    def __init__(
        self,
        *,
        research_adapter: ResearchRunnerAdapter,
        execution_adapter: ResearchRunnerExecutionAdapter,
        aggregator: ResultsAggregatorAdapter,
        gate: CandidateGate,
        policy: HoldoutEvaluationPolicy | None = None,
    ) -> None:
        self._research = research_adapter
        self._execution = execution_adapter
        self._aggregator = aggregator
        self._gate = gate
        self._policy = policy or HoldoutEvaluationPolicy()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        limit = context.request.budget.stage_experiment_limits.get(FactoryStage.HOLDOUT.value)
        if limit is None:
            raise CompatibilityError("holdout requires an explicit stage_experiment_limits budget")
        return StageBudgetRequest(experiments=limit)

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        robustness_artifact = _mapping(
            context.load_artifact(FactoryStage.ROBUSTNESS.value),
            "robustness artifact",
        )
        walk_artifact = _mapping(
            context.load_artifact(FactoryStage.WALK_FORWARD.value),
            "walk-forward artifact",
        )
        walk_by_candidate = {
            decode_candidate_strategy(record.get("candidate")).candidate_id: record
            for record in (
                _mapping(raw, "walk-forward candidate")
                for raw in _sequence(walk_artifact.get("candidates"), "walk-forward candidates")
            )
        }
        finalists: list[tuple[Any, RobustnessReport, Mapping[str, Any]]] = []
        for raw in _sequence(robustness_artifact.get("candidates"), "robustness candidates"):
            record = _mapping(raw, "robustness candidate")
            candidate = decode_candidate_strategy(record.get("candidate"))
            robustness_report = decode_robustness_report(record.get("robustness_report"))
            if robustness_report.passed:
                walk = walk_by_candidate.get(candidate.candidate_id)
                if walk is None:
                    raise CompatibilityError("robustness finalist has no walk-forward lineage")
                finalists.append((candidate, robustness_report, walk))
        if not finalists:
            return StageOutcome(
                artifact_type="holdout_results",
                artifact={
                    "schema_version": 1,
                    "candidates": (),
                    "passed_candidate_count": 0,
                    "holdout_opened": False,
                },
                reason_codes=("NO_ROBUSTNESS_FINALIST_FOR_HOLDOUT",),
                subject_id=context.checkpoint.run_id,
                finalists_selected=0,
            )
        if len(finalists) > context.request.budget.maximum_finalists:
            raise CompatibilityError("holdout finalist count exceeds configured budget")
        stage_limit = context.request.budget.stage_experiment_limits[FactoryStage.HOLDOUT.value]
        consumed = 0
        outputs: list[dict[str, Any]] = []
        for candidate, robustness, walk in finalists:
            split = _split_from_mapping(walk.get("split"))
            prepared = self._research.build_walk_forward_plan(
                request=context.request,
                factory_run_id=context.checkpoint.run_id,
                manifest_id=context.manifest.manifest_id,
                candidate=candidate,
                split=split,
            )
            final_jobs = _positive_int(
                prepared.factory_plan.resource_limits.get("final_job_count"),
                "holdout final_job_count",
            )
            if consumed + final_jobs > stage_limit:
                raise CompatibilityError("holdout job count exceeds the reserved stage budget")
            consumed += final_jobs
            experiment_id = _text(walk.get("experiment_id"), "experiment_id")
            self._execution.restore_submission(
                prepared,
                expected_experiment_id=experiment_id,
            )
            native_hash = _text(
                walk.get("native_configuration_hash"),
                "native_configuration_hash",
            )
            gate = self._gate.evaluate(candidate)
            admission = HoldoutAdmissionController(
                self._execution,
                maximum_finalists=1,
            ).authorize(
                experiment_id=experiment_id,
                candidates=(candidate,),
                gates=(gate,),
                robustness_reports=(robustness,),
                producer_configuration_hashes={candidate.candidate_id: native_hash},
                issued_at=context.checkpoint.updated_at,
            )
            authorization = admission.native_authorization
            grant_id = _text(getattr(authorization, "grant_id", None), "grant_id")
            activation = self._execution.activate_final_test(
                experiment_id,
                grant_id=grant_id,
                activated_at=context.checkpoint.updated_at,
            )
            snapshot = _terminal_research_snapshot(
                self._execution.resume(experiment_id),
                operation="independent holdout",
                success=self._SUCCESS,
            )
            population = self._aggregator.candidates_for_experiment(
                experiment_id,
                generated_at=context.checkpoint.updated_at,
            )
            matching = tuple(
                item
                for item in population
                if item.configuration_hash == candidate.configuration_hash
            )
            if len(matching) != 1:
                raise CompatibilityError(
                    "Results Aggregator must return exactly one holdout evaluation"
                )
            metrics = matching[0].evaluation.metrics
            profit_factor = _holdout_metric(metrics, "profit_factor")
            maximum_drawdown = abs(_holdout_metric(metrics, "max_drawdown"))
            trade_count_decimal = _holdout_metric(metrics, "trade_count")
            trade_count = int(trade_count_decimal)
            if Decimal(trade_count) != trade_count_decimal or trade_count < 0:
                raise CompatibilityError("holdout trade_count must be a non-negative integer")
            passed = (
                profit_factor >= self._policy.minimum_profit_factor
                and maximum_drawdown <= context.request.maximum_drawdown_fraction
                and trade_count >= context.request.minimum_trade_count
            )
            holdout_report = HoldoutReport(
                report_id=(
                    "holdout-"
                    + canonical_sha256(
                        (
                            context.checkpoint.run_id,
                            candidate.candidate_id,
                            grant_id,
                            matching[0].evaluation.evaluation_id,
                        )
                    )[:24]
                ),
                candidate_id=candidate.candidate_id,
                period=split.holdout,
                dataset_fingerprint=context.request.dataset.fingerprint,
                metrics={
                    "profit_factor": profit_factor,
                    "max_drawdown": maximum_drawdown,
                    "trade_count": trade_count,
                    "source_evaluation_id": matching[0].evaluation.evaluation_id,
                },
                passed=passed,
                opened_once=True,
                generated_at=context.checkpoint.updated_at,
            )
            outputs.append(
                {
                    "candidate": candidate,
                    "robustness_report": robustness,
                    "holdout_report": holdout_report,
                    "authorization": activation.authorization,
                    "activation_run_ids": activation.run_ids,
                    "terminal_status": snapshot.status,
                    "experiment_id": experiment_id,
                    "native_configuration_hash": native_hash,
                    "split": split,
                }
            )
        passed_count = sum(1 for item in outputs if item["holdout_report"].passed)
        return StageOutcome(
            artifact_type="holdout_results",
            artifact={
                "schema_version": 1,
                "candidates": tuple(outputs),
                "passed_candidate_count": passed_count,
                "holdout_opened": True,
                "consumed_job_budget": consumed,
            },
            reason_codes=(
                "FINAL_TEST_GRANT_CONSUMED_ONCE",
                "INDEPENDENT_HOLDOUT_COMPLETED",
                (
                    "HOLDOUT_FINALISTS_AVAILABLE"
                    if passed_count
                    else "HOLDOUT_REJECTED_ALL_CANDIDATES"
                ),
            ),
            subject_id=context.checkpoint.run_id,
            finalists_selected=passed_count,
        )


@dataclass(frozen=True, slots=True)
class SimplicitySelectionPolicy:
    """Transparent tolerances for preferring a simpler holdout-passing candidate."""

    profit_factor_tolerance: Decimal = Decimal("0.05")
    drawdown_tolerance: Decimal = Decimal("0.02")
    trade_count_drop_fraction: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        for name in ("profit_factor_tolerance", "drawdown_tolerance"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ContractError(f"{name} must be a finite non-negative Decimal")
        value = self.trade_count_drop_fraction
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or not Decimal("0") <= value < Decimal("1")
        ):
            raise ContractError("trade_count_drop_fraction must be a finite Decimal in [0, 1)")


class SimplicitySelectionHandler:
    """Choose among holdout passers without collapsing evidence into one score."""

    def __init__(self, policy: SimplicitySelectionPolicy | None = None) -> None:
        self._policy = policy or SimplicitySelectionPolicy()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.HOLDOUT.value),
            "holdout artifact",
        )
        records: list[dict[str, Any]] = []
        for raw in _sequence(artifact.get("candidates"), "holdout candidates"):
            record = _mapping(raw, "holdout candidate")
            candidate = decode_candidate_strategy(record.get("candidate"))
            robustness = decode_robustness_report(record.get("robustness_report"))
            holdout = decode_holdout_report(record.get("holdout_report"))
            if not robustness.passed or not holdout.passed:
                continue
            if candidate.evaluation is None:
                raise CompatibilityError("simplicity selection requires a candidate evaluation")
            if (
                robustness.candidate_id != candidate.candidate_id
                or holdout.candidate_id != candidate.candidate_id
            ):
                raise CompatibilityError("simplicity selection candidate evidence is cross-linked")
            profit_factor = _required_numeric_metric(
                holdout.metrics, "profit_factor", source="holdout"
            )
            drawdown = abs(
                _required_numeric_metric(holdout.metrics, "max_drawdown", source="holdout")
            )
            trade_count = _required_nonnegative_integer_metric(
                holdout.metrics, "trade_count", source="holdout"
            )
            stability = _required_numeric_metric(
                candidate.evaluation.metrics,
                "parameter_stability",
                source="candidate evaluation",
            )
            records.append(
                {
                    "candidate": candidate,
                    "robustness_report": robustness,
                    "holdout_report": holdout,
                    "authorization": record.get("authorization"),
                    "experiment_id": record.get("experiment_id"),
                    "activation_run_ids": record.get("activation_run_ids", ()),
                    "native_configuration_hash": record.get("native_configuration_hash"),
                    "split": record.get("split"),
                    "profit_factor": profit_factor,
                    "max_drawdown": drawdown,
                    "trade_count": trade_count,
                    "parameter_stability": stability,
                    "complexity": candidate.evaluation.complexity,
                    "runtime_module_count": len(candidate.architecture.runtime_modules),
                    "parameter_count": len(candidate.parameters),
                }
            )
        if not records:
            return StageOutcome(
                artifact_type="simplicity_selection",
                artifact={
                    "schema_version": 1,
                    "selected": None,
                    "alternatives": (),
                    "policy": self._policy,
                },
                reason_codes=("NO_HOLDOUT_PASSER_FOR_SELECTION",),
                subject_id=context.checkpoint.run_id,
                finalists_selected=0,
            )

        performance_anchor = min(
            records,
            key=lambda item: (
                -item["profit_factor"],
                item["max_drawdown"],
                -item["trade_count"],
                item["candidate"].candidate_id,
            ),
        )
        minimum_trades = max(
            context.request.minimum_trade_count,
            int(
                Decimal(performance_anchor["trade_count"])
                * (Decimal("1") - self._policy.trade_count_drop_fraction)
            ),
        )
        near_anchor = tuple(
            item
            for item in records
            if item["profit_factor"]
            >= performance_anchor["profit_factor"] - self._policy.profit_factor_tolerance
            and item["max_drawdown"]
            <= performance_anchor["max_drawdown"] + self._policy.drawdown_tolerance
            and item["trade_count"] >= minimum_trades
        )
        if not near_anchor:
            raise CompatibilityError(
                "simplicity selection produced no candidate within its anchor tolerances"
            )
        selected = min(
            near_anchor,
            key=lambda item: (
                item["complexity"],
                item["runtime_module_count"],
                item["parameter_count"],
                -item["parameter_stability"],
                item["max_drawdown"],
                -item["profit_factor"],
                item["candidate"].candidate_id,
            ),
        )
        alternatives: list[dict[str, Any]] = []
        for item in sorted(records, key=lambda value: value["candidate"].candidate_id):
            if item is selected:
                continue
            reasons: list[str] = []
            if item not in near_anchor:
                if (
                    item["profit_factor"]
                    < performance_anchor["profit_factor"] - self._policy.profit_factor_tolerance
                ):
                    reasons.append("OUTSIDE_PROFIT_FACTOR_TOLERANCE")
                if (
                    item["max_drawdown"]
                    > performance_anchor["max_drawdown"] + self._policy.drawdown_tolerance
                ):
                    reasons.append("OUTSIDE_DRAWDOWN_TOLERANCE")
                if item["trade_count"] < minimum_trades:
                    reasons.append("INSUFFICIENT_RELATIVE_TRADE_COUNT")
            else:
                if item["complexity"] > selected["complexity"]:
                    reasons.append("HIGHER_COMPLEXITY")
                if item["runtime_module_count"] > selected["runtime_module_count"]:
                    reasons.append("MORE_RUNTIME_MODULES")
                if item["parameter_count"] > selected["parameter_count"]:
                    reasons.append("MORE_FREE_PARAMETERS")
                if item["parameter_stability"] < selected["parameter_stability"]:
                    reasons.append("LOWER_PARAMETER_STABILITY")
            if not reasons:
                reasons.append("DETERMINISTIC_TIE_BREAK")
            alternatives.append(
                {
                    "candidate_id": item["candidate"].candidate_id,
                    "reason_codes": tuple(reasons),
                    "dimensions": _simplicity_dimensions(item),
                }
            )
        selected_reason = (
            "SIMPLER_CANDIDATE_WITHIN_HOLDOUT_TOLERANCE"
            if selected is not performance_anchor
            else "BEST_HOLDOUT_EVIDENCE_RETAINED"
        )
        return StageOutcome(
            artifact_type="simplicity_selection",
            artifact={
                "schema_version": 1,
                "selected": {
                    "candidate": selected["candidate"],
                    "robustness_report": selected["robustness_report"],
                    "holdout_report": selected["holdout_report"],
                    "authorization": selected["authorization"],
                    "experiment_id": selected["experiment_id"],
                    "activation_run_ids": selected["activation_run_ids"],
                    "native_configuration_hash": selected["native_configuration_hash"],
                    "split": selected["split"],
                    "selection_reason": selected_reason,
                    "dimensions": _simplicity_dimensions(selected),
                },
                "performance_anchor_candidate_id": performance_anchor["candidate"].candidate_id,
                "alternatives": tuple(alternatives),
                "policy": self._policy,
                "comparison_dimensions": (
                    "holdout_profit_factor",
                    "holdout_max_drawdown",
                    "holdout_trade_count",
                    "complexity",
                    "runtime_module_count",
                    "parameter_count",
                    "parameter_stability",
                ),
            },
            reason_codes=(
                "EXPLAINABLE_MULTI_DIMENSION_SELECTION",
                selected_reason,
            ),
            subject_id=selected["candidate"].candidate_id,
            finalists_selected=1,
        )


def _simplicity_dimensions(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "holdout_profit_factor": item["profit_factor"],
        "holdout_max_drawdown": item["max_drawdown"],
        "holdout_trade_count": item["trade_count"],
        "complexity": item["complexity"],
        "runtime_module_count": item["runtime_module_count"],
        "parameter_count": item["parameter_count"],
        "parameter_stability": item["parameter_stability"],
    }


def _required_numeric_metric(metrics: Mapping[str, Any], metric: str, *, source: str) -> Decimal:
    if metric not in metrics:
        raise CompatibilityError(f"{source} is missing required metric: {metric}")
    value = metrics[metric]
    if isinstance(value, bool):
        raise CompatibilityError(f"{source} metric {metric} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CompatibilityError(f"{source} metric {metric} must be numeric") from exc
    if not result.is_finite():
        raise CompatibilityError(f"{source} metric {metric} must be finite")
    return result


def _required_nonnegative_integer_metric(
    metrics: Mapping[str, Any], metric: str, *, source: str
) -> int:
    rendered = _required_numeric_metric(metrics, metric, source=source)
    value = int(rendered)
    if Decimal(value) != rendered or value < 0:
        raise CompatibilityError(f"{source} metric {metric} must be a non-negative integer")
    return value


class FinalAuditHandler:
    """Submit a durable exact-schema envelope to the independent Strategy Audit port."""

    def __init__(
        self,
        audit_port: StrategyAuditPort,
        *,
        envelope_builder: FactoryAuditEnvelopeBuilder | None = None,
    ) -> None:
        if not callable(getattr(audit_port, "audit", None)):
            raise TypeError("audit_port must implement audit()")
        contract_version = getattr(audit_port, "contract_version", None)
        if not isinstance(contract_version, str) or not contract_version.strip():
            raise TypeError("audit_port must expose a non-blank contract_version")
        self._audit = audit_port
        self._envelopes = envelope_builder or FactoryAuditEnvelopeBuilder(
            dataset_source=UnavailableDatasetAuditLineageSource(),
            results_source=UnavailableResultsAuditEvidenceSource(),
            contract_version=contract_version,
        )
        if self._envelopes.contract_version != contract_version:
            raise TypeError("envelope_builder and audit_port contract versions must match")

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        selection_artifact_id = context.artifact_id(FactoryStage.SIMPLICITY_SELECTION.value)
        selection = _mapping(
            context.load_artifact(FactoryStage.SIMPLICITY_SELECTION.value),
            "simplicity selection artifact",
        )
        selected_raw = selection.get("selected")
        if selected_raw is None:
            return StageOutcome(
                artifact_type="final_audit",
                artifact={
                    "schema_version": 1,
                    "evidence_package_reference": None,
                    "evidence_artifact_id": None,
                    "evidence_hash": canonical_sha256(selection),
                    "audit_decision": None,
                    "selected": None,
                },
                reason_codes=("NO_CANDIDATE_FOR_FINAL_AUDIT",),
                subject_id=context.checkpoint.run_id,
                finalists_selected=0,
            )
        selected = _mapping(selected_raw, "selected candidate evidence")
        candidate = decode_candidate_strategy(selected.get("candidate"))
        robustness = decode_robustness_report(selected.get("robustness_report"))
        holdout = decode_holdout_report(selected.get("holdout_report"))
        if not robustness.passed or not holdout.passed:
            raise CompatibilityError("final audit candidate must pass robustness and holdout")
        experiment_id = selected.get("experiment_id")
        if (
            not isinstance(experiment_id, str)
            or not experiment_id.strip()
            or experiment_id != experiment_id.strip()
        ):
            raise CompatibilityError("final audit requires a selected experiment_id")
        split = _split_from_mapping(selected.get("split"))
        package_intent: Mapping[str, Any] | None = None
        if getattr(self._audit, "contract_version", None) == AUDIT_CONTRACT_VERSION_V2:
            draft = StrategyPackageBuilder().build(
                **_package_build_arguments(
                    context,
                    candidate,
                    robustness,
                    holdout,
                )
            )
            package_intent = create_package_intent(draft, context.request).as_mapping()
        envelope = self._envelopes.build(
            request=context.request,
            manifest=context.manifest,
            candidate=candidate,
            robustness=robustness,
            holdout=holdout,
            periods=AuditResearchPeriods(
                train=split.train,
                validation=split.validation,
                test=split.test,
                holdout=split.holdout,
            ),
            experiment_id=experiment_id,
            package_intent=package_intent,
        )
        evidence_artifact_id = context.store_artifact(
            artifact_type="strategy_audit_envelope",
            value=envelope,
        )
        evidence_reference = EVIDENCE_REFERENCE_PREFIX + evidence_artifact_id
        evidence_hash = canonical_sha256(envelope)
        raw_decision = self._audit.audit(evidence_reference)
        decision = (
            raw_decision
            if isinstance(raw_decision, StrategyAuditDecision)
            else decode_strategy_audit_decision(raw_decision)
        )
        if decision.contract_version != self._audit.contract_version:
            raise CompatibilityError("Strategy Audit contract version mismatch")
        if decision.candidate_id != candidate.candidate_id:
            raise CompatibilityError("Strategy Audit decision belongs to another candidate")
        if decision.evidence_package_reference != evidence_reference:
            raise CompatibilityError("Strategy Audit decision references different evidence")
        if decision.evidence_hash != evidence_hash:
            raise CompatibilityError("Strategy Audit evidence hash mismatch")
        if decision.issued_at < holdout.generated_at:
            raise CompatibilityError("Strategy Audit decision predates holdout evidence")
        if decision.status is AdmissionStatus.APPROVED_FOR_PAPER and (
            not robustness.passed or not holdout.passed
        ):
            raise CompatibilityError(
                "Strategy Audit cannot approve evidence that failed prior gates"
            )
        final_artifact: dict[str, Any] = {
            "schema_version": 1,
            "source_selection_artifact_id": selection_artifact_id,
            "evidence_package_reference": evidence_reference,
            "evidence_artifact_id": evidence_artifact_id,
            "evidence_hash": evidence_hash,
            "audit_decision": decision,
            "selected": selected,
        }
        if package_intent is not None:
            final_artifact["package_intent"] = package_intent
        return StageOutcome(
            artifact_type="final_audit",
            artifact=final_artifact,
            reason_codes=(
                "EXACT_AUDIT_ENVELOPE_PERSISTED",
                "INDEPENDENT_AUDIT_DECISION_RECORDED",
                f"AUDIT_{decision.status.value}",
            ),
            subject_id=candidate.candidate_id,
            finalists_selected=(1 if decision.status is AdmissionStatus.APPROVED_FOR_PAPER else 0),
        )


class StrategyPackageHandler:
    """Build a hash-bound paper-only package from the audited finalist."""

    def __init__(self, builder: StrategyPackageBuilder | None = None) -> None:
        self._builder = builder or StrategyPackageBuilder()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        audit_artifact_id = context.artifact_id(FactoryStage.FINAL_AUDIT.value)
        artifact = _mapping(
            context.load_artifact(FactoryStage.FINAL_AUDIT.value),
            "final audit artifact",
        )
        selected_raw = artifact.get("selected")
        audit_raw = artifact.get("audit_decision")
        if selected_raw is None or audit_raw is None:
            return StageOutcome(
                artifact_type="strategy_package",
                artifact={
                    "schema_version": 1,
                    "strategy_package": None,
                    "admission_status": AdmissionStatus.INSUFFICIENT_EVIDENCE.value,
                    "reason": "NO_AUDITED_FINALIST",
                },
                reason_codes=("NO_AUDITED_FINALIST_FOR_PACKAGE",),
                subject_id=context.checkpoint.run_id,
                finalists_selected=0,
            )
        selected = _mapping(selected_raw, "audited selection")
        candidate = decode_candidate_strategy(selected.get("candidate"))
        robustness = decode_robustness_report(selected.get("robustness_report"))
        holdout = decode_holdout_report(selected.get("holdout_report"))
        audit = decode_strategy_audit_decision(audit_raw)
        intent_raw = artifact.get("package_intent")
        package_intent = intent_raw if isinstance(intent_raw, Mapping) else None
        package = self._builder.build(
            **_package_build_arguments(
                context,
                candidate,
                robustness,
                holdout,
                audit=audit,
                audit_artifact_id=audit_artifact_id,
            ),
            package_intent=package_intent,
        )
        self._builder.verify(package)
        return StageOutcome(
            artifact_type="strategy_package",
            artifact={
                "schema_version": 1,
                "strategy_package": package,
                "admission_status": package.admission_status.value,
                "audit_decision_hash": audit.decision_hash,
            },
            reason_codes=(
                "STRATEGY_PACKAGE_HASH_VERIFIED",
                "LIVE_TRADING_NOT_AUTHORIZED",
                f"PACKAGE_{package.admission_status.value}",
            ),
            subject_id=package.strategy_id,
            finalists_selected=(
                1 if package.admission_status is AdmissionStatus.APPROVED_FOR_PAPER else 0
            ),
        )


class PaperAdmissionHandler:
    """Emit a paper-runtime admission descriptor; never authorize live execution."""

    def __init__(self, builder: StrategyPackageBuilder | None = None) -> None:
        self._builder = builder or StrategyPackageBuilder()

    def budget_request(self, context: FactoryStageContext) -> StageBudgetRequest:
        return StageBudgetRequest()

    def execute(self, context: FactoryStageContext) -> StageOutcome:
        artifact = _mapping(
            context.load_artifact(FactoryStage.PACKAGE.value),
            "strategy package artifact",
        )
        package_raw = artifact.get("strategy_package")
        if package_raw is None:
            return StageOutcome(
                artifact_type="paper_admission",
                artifact={
                    "schema_version": 1,
                    "status": AdmissionStatus.INSUFFICIENT_EVIDENCE.value,
                    "paper_configuration": None,
                    "live_trading_authorized": False,
                },
                reason_codes=("PAPER_ADMISSION_WITHHELD_NO_PACKAGE",),
                subject_id=context.checkpoint.run_id,
                finalists_selected=0,
            )
        package = decode_strategy_package(package_raw)
        self._builder.verify(package)
        approved = package.admission_status is AdmissionStatus.APPROVED_FOR_PAPER
        module_graph = tuple(
            {
                "runtime_key": module.runtime_key,
                "import_path": module.import_path,
                "version": module.runtime_component_version,
                "parameters": package.parameters.get(module.runtime_key, {}),
            }
            for module in package.architecture.runtime_modules
        )
        paper_configuration = (
            {
                **dict(package.paper_configuration),
                "package_hash": package.package_hash,
                "strategy_id": package.strategy_id,
                "module_graph": module_graph,
                "execution_mode": "paper",
                "live_trading_authorized": False,
            }
            if approved
            else None
        )
        return StageOutcome(
            artifact_type="paper_admission",
            artifact={
                "schema_version": 1,
                "status": package.admission_status.value,
                "strategy_id": package.strategy_id,
                "package_hash": package.package_hash,
                "paper_configuration": paper_configuration,
                "live_trading_authorized": False,
            },
            reason_codes=(
                "PAPER_ADMISSION_GRANTED" if approved else "PAPER_ADMISSION_WITHHELD",
                "LIVE_TRADING_NOT_AUTHORIZED",
            ),
            subject_id=package.strategy_id,
            finalists_selected=1 if approved else 0,
        )


def default_research_split(
    request: StrategyFactoryRequest,
    *,
    gap_seconds: int = 0,
) -> ResearchSplitPlan:
    """Create deterministic chronological train/validation/test/holdout ranges."""

    if isinstance(gap_seconds, bool) or not isinstance(gap_seconds, int) or gap_seconds < 0:
        raise ContractError("gap_seconds must be a non-negative int")
    period = request.dataset.period
    if request.explicit_holdout is not None:
        holdout = request.explicit_holdout
        if holdout.start < period.start or holdout.end > period.end:
            raise CompatibilityError("explicit holdout lies outside the dataset period")
        train, validation, test = _partition(
            period.start,
            holdout.start,
            (60, 20, 20),
        )
    else:
        train, validation, test, holdout = _partition(
            period.start,
            period.end,
            (60, 20, 10, 10),
        )
    return ResearchSplitPlan(
        train=train,
        validation=validation,
        test=test,
        holdout=holdout,
        gap_seconds=gap_seconds,
    )


def _terminal_research_snapshot(
    result: Any,
    *,
    operation: str,
    success: frozenset[str],
) -> Any:
    failure = getattr(result, "failure", None)
    if failure is not None:
        reason = getattr(failure, "reason_code", "UNKNOWN_EXTERNAL_FAILURE")
        if getattr(failure, "retryable", False):
            raise StageBlockedError(f"{operation} is retryable: {reason}")
        raise CompatibilityError(f"{operation} failed: {reason}")
    snapshot = getattr(result, "snapshot", None)
    if snapshot is None:
        raise CompatibilityError(f"{operation} returned no snapshot")
    if snapshot.has_nonterminal_runs or snapshot.status not in success:
        raise StageBlockedError(f"{operation} is not terminal: {snapshot.status}")
    return snapshot


def _holdout_metric(metrics: Mapping[str, Any], metric: str) -> Decimal:
    tokens = ("holdout", "final_untouched", "untouched_test")
    matches = [
        value
        for key, value in metrics.items()
        if any(token in key.lower() for token in tokens)
        and (key == metric or key.endswith(f".{metric}"))
    ]
    if len(matches) != 1:
        raise CompatibilityError(f"holdout metric {metric} is missing or ambiguous")
    value = matches[0]
    if isinstance(value, bool):
        raise CompatibilityError(f"holdout metric {metric} must be numeric")
    try:
        rendered = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CompatibilityError(f"holdout metric {metric} must be numeric") from exc
    if not rendered.is_finite():
        raise CompatibilityError(f"holdout metric {metric} must be finite")
    return rendered


def _optional_string_sequence(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(sorted(set(_text(item, name) for item in _sequence(value, name))))


def _partition(
    start: datetime,
    end: datetime,
    weights: tuple[int, ...],
) -> tuple[TimeRange, ...]:
    if end <= start or not weights or any(weight <= 0 for weight in weights):
        raise CompatibilityError("research period cannot be partitioned")
    duration = end - start
    total_microseconds = (
        duration.days * 86_400_000_000 + duration.seconds * 1_000_000 + duration.microseconds
    )
    if total_microseconds < len(weights):
        raise CompatibilityError("research period is too short for requested splits")
    total_weight = sum(weights)
    boundaries = [start]
    cumulative = 0
    for weight in weights[:-1]:
        cumulative += weight
        offset = total_microseconds * cumulative // total_weight
        boundaries.append(start + timedelta(microseconds=offset))
    boundaries.append(end)
    ranges = tuple(
        TimeRange(left, right) for left, right in zip(boundaries, boundaries[1:], strict=False)
    )
    if any(item.end <= item.start for item in ranges):
        raise CompatibilityError("research split produced an empty range")
    return ranges


def _split_mapping(split: ResearchSplitPlan) -> Mapping[str, Any]:
    return {
        "train": split.train,
        "validation": split.validation,
        "test": split.test,
        "holdout": split.holdout,
        "gap_seconds": split.gap_seconds,
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{name} must be a sequence")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CompatibilityError(f"{name} must be a positive int")
    return value


def _split_from_mapping(value: object) -> ResearchSplitPlan:
    mapping = _mapping(value, "research split")
    expected = {"train", "validation", "test", "holdout", "gap_seconds"}
    if set(mapping) != expected:
        raise ContractError("research split has an invalid schema")
    return ResearchSplitPlan(
        train=_time_range(mapping["train"], "train"),
        validation=_time_range(mapping["validation"], "validation"),
        test=_time_range(mapping["test"], "test"),
        holdout=_time_range(mapping["holdout"], "holdout"),
        gap_seconds=_nonnegative_int(mapping["gap_seconds"], "gap_seconds"),
    )


def _time_range(value: object, name: str) -> TimeRange:
    if isinstance(value, TimeRange):
        return value
    mapping = _mapping(value, f"{name} range")
    if set(mapping) != {"start", "end"}:
        raise ContractError(f"{name} range has an invalid schema")
    start = _datetime(mapping["start"], f"{name}.start")
    end = _datetime(mapping["end"], f"{name}.end")
    return TimeRange(start, end)


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ContractError(f"{name} must be ISO-8601") from exc
    else:
        raise ContractError(f"{name} must be datetime or ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware")
    return parsed


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ContractError(f"{name} must be a non-blank trimmed string")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative int")
    return value


def _package_build_arguments(
    context: FactoryStageContext,
    candidate: CandidateStrategy,
    robustness: RobustnessReport,
    holdout: HoldoutReport,
    *,
    audit: StrategyAuditDecision | None = None,
    audit_artifact_id: str | None = None,
) -> dict[str, Any]:
    """Share pre-audit and post-audit package field assembly exactly."""

    if candidate.evaluation is None:
        raise CompatibilityError("Strategy Package requires candidate evaluation")
    selection = _mapping(
        context.load_artifact(FactoryStage.SIMPLICITY_SELECTION.value),
        "simplicity selection artifact",
    )
    selected = _mapping(selection.get("selected"), "selected candidate evidence")
    split = _split_from_mapping(selected.get("split"))
    execution_objective = context.request.objectives.get("execution_assumptions", {})
    execution_assumptions = dict(_mapping(execution_objective, "objectives.execution_assumptions"))
    risk_objective = context.request.objectives.get("risk_policy", {})
    risk_policy = {
        "research_risk_fraction": context.request.research_risk_fraction,
        "initial_capital": context.request.initial_capital,
        **dict(_mapping(risk_objective, "objectives.risk_policy")),
    }
    weaknesses = set(candidate.evaluation.instability_flags)
    weaknesses.update(
        _optional_string_sequence(
            context.request.objectives.get("known_weaknesses", ()),
            "objectives.known_weaknesses",
        )
    )
    disabled_regimes = _optional_string_sequence(
        context.request.objectives.get("disabled_regimes", ()),
        "objectives.disabled_regimes",
    )
    evidence_links = set(candidate.evaluation.evidence_references)
    evidence_links.update(
        link for module in candidate.architecture.runtime_modules for link in module.evidence_links
    )
    evidence_links.update((robustness.report_id, holdout.report_id))
    key_results: dict[str, Any] = {
        "candidate_metrics": candidate.evaluation.metrics,
        "holdout_metrics": holdout.metrics,
        "pareto_front": candidate.evaluation.pareto_front,
        "complexity": candidate.evaluation.complexity,
    }
    if audit is not None:
        if audit_artifact_id is None:
            raise CompatibilityError("audited package requires its audit artifact identity")
        evidence_links.update(
            {
                audit.audit_id,
                audit.decision_hash,
                audit.evidence_package_reference,
                audit_artifact_id,
            }
        )
        key_results["audit_status"] = audit.status.value
    strategy_id = f"strategy-{context.request.symbol.lower()}-{candidate.configuration_hash[:16]}"
    version = context.request.objectives.get("strategy_version", "0.1.0")
    if not isinstance(version, str) or not version.strip():
        raise ContractError("objectives.strategy_version must be a string")
    return {
        "request": context.request,
        "candidate": candidate,
        "strategy_id": strategy_id,
        "version": version,
        "commit_sha": context.manifest.commit_sha,
        "research_periods": {
            "train": split.train,
            "validation": split.validation,
            "test": split.test,
            "holdout": split.holdout,
        },
        "robustness": robustness,
        "holdout": holdout,
        "key_results": key_results,
        "known_weaknesses": tuple(sorted(weaknesses)),
        "disabled_regimes": disabled_regimes,
        "evidence_links": tuple(sorted(evidence_links)),
        "paper_configuration": {
            "factory_run_id": context.checkpoint.run_id,
            "factory_manifest_id": context.manifest.manifest_id,
            "candidate_id": candidate.candidate_id,
            "symbol": context.request.symbol,
            "market_type": context.request.market_type.value,
        },
        "risk_policy": risk_policy,
        "execution_assumptions": {
            "market_type": context.request.market_type.value,
            "exchange": context.request.exchange,
            **execution_assumptions,
        },
        "audit_decision": audit,
    }
