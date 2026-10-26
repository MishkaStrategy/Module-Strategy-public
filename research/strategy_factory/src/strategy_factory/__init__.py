"""Automatic Strategy Builder for the Modular Strategy research stack."""

from .adapters import (
    PreparedResearchPlan,
    ResearchControlResult,
    ResearchExperimentSnapshot,
    ResearchFinalTestActivation,
    ResearchFinalTestAuthorization,
    ResearchFrozenConfiguration,
    ResearchRunnerAdapter,
    ResearchRunnerExecutionAdapter,
    ResearchSplitPlan,
    ResearchStageActivation,
    ResearchSubmission,
    ResultsAggregatorAdapter,
    StrategyAuditPort,
    StrategyPackageSourcePort,
    StructuredExternalFailure,
    UnavailableStrategyAuditPort,
    UnavailableStrategyPackageSourcePort,
)
from .application import (
    ApplicationRunResult,
    RuntimeAssembly,
    StrategyFactoryApplication,
)
from .artifacts import ApprovedStrategyPackageLoader, ContentAddressedArtifactStore
from .audit_bridge import IndependentStrategyAuditPort
from .audit_envelope import (
    AUDIT_CONTRACT_VERSION,
    AUDIT_CONTRACT_VERSION_V2,
    EVIDENCE_REFERENCE_PREFIX,
    AuditResearchPeriods,
    DatasetAuditLineage,
    DatasetAuditLineageSource,
    FactoryArtifactEvidenceResolver,
    FactoryAuditEnvelopeBuilder,
    ResultsAuditEvidence,
    ResultsAuditEvidenceSource,
    UnavailableDatasetAuditLineageSource,
    UnavailableResultsAuditEvidenceSource,
)
from .backtest_config import (
    BacktestEnginePolicy,
    OfficialBacktestConfigFactory,
    TrustedRuntimeModule,
)
from .budget import BudgetLedger, BudgetSnapshot
from .catalog_v06 import (
    CatalogBuildIssue,
    ModuleEvidenceRecord,
    ModuleRoleSpec,
    TrustedModuleSpec,
    TrustedRepositoryCatalogBuilder,
    V06CatalogBuildReport,
    v06_trusted_specs,
)
from .checkpoint import AtomicCheckpointStore
from .contract_codec import (
    decode_experiment_plan,
    decode_holdout_report,
    decode_instrument_profile,
    decode_market_regime_definition,
    decode_module_reference,
    decode_parameter_space,
    decode_strategy_architecture,
    decode_strategy_audit_decision,
    decode_strategy_hypothesis,
    decode_strategy_package,
)
from .contracts import *  # noqa: F403
from .coordinator import (
    CoordinatorStepResult,
    FactoryCoordinator,
    FactoryStageContext,
    StageBudgetRequest,
    StageHandler,
    StageOutcome,
)
from .dataset import (
    DatasetAnalysis,
    DatasetAnalyzer,
    DatasetValidationPolicy,
    OhlcvColumns,
)
from .decision_log import DecisionLog, DecisionLogEntry
from .grammar import (
    ArchitectureGenerationResult,
    ArchitectureRejection,
    GrammarPolicy,
    StrategyGrammar,
)
from .hypothesis import HypothesisGenerator, RegimeLibrary
from .idempotency import IdempotencyLedger
from .observability import FactoryMetrics, JsonLogFormatter
from .package import StrategyPackageBuilder
from .package_intent import (
    PACKAGE_INTENT_SCHEMA_VERSION,
    PackageIntent,
    create_package_intent,
    package_projection,
    verify_package_intent,
)
from .planner import ArchitecturePlanner
from .promotion import (
    BaselineAwareCandidateSelector,
    BaselineComparisonPolicy,
    PromotionSelection,
    ResultsAggregatorCandidateSelector,
)
from .ranking import (
    Constraint,
    Objective,
    ObjectiveDirection,
    ParetoRanker,
    RankedCandidate,
    RankingResult,
)
from .registry import ModuleRegistrationMetadata, StrategyModuleCatalog
from .results_publication import (
    PublishedRegistryRun,
    ResearchResultsPublicationAdapter,
    ResultsPublicationPolicy,
)
from .run import StrategyFactoryRun
from .runtime_binding import (
    PartialStrategyPackageRuntimeBinding,
    StrategyPackageRuntimeBinder,
)
from .stage_handlers import (
    ArchitectureGenerationHandler,
    BaselinePackageValidationHandler,
    ExperimentPlanningHandler,
    ExperimentPlanningPolicy,
    FinalAuditHandler,
    HoldoutEvaluationPolicy,
    HypothesisGenerationHandler,
    IndependentHoldoutHandler,
    OptimizationPromotionHandler,
    PackageSeededArchitectureHandler,
    PackageSeededExperimentPlanningHandler,
    PackageSeededHypothesisHandler,
    PaperAdmissionHandler,
    RegimeDiscoveryHandler,
    RobustnessEvidenceHandler,
    SimplicitySelectionHandler,
    SimplicitySelectionPolicy,
    StrategyPackageHandler,
    StructuralScreeningHandler,
    StructuralScreeningPolicy,
    WalkForwardValidationHandler,
    default_research_split,
)
from .validation import (
    CandidateGate,
    CandidateGatePolicy,
    CandidateGateResult,
    HoldoutAdmissionController,
    HoldoutAdmissionDecision,
    HoldoutIsolationGuard,
    MultipleTestingGuard,
    MultipleTestingResult,
)

__version__ = "0.1.0"

__all__ = tuple(sorted(name for name in globals() if not name.startswith("_"))) + ("__version__",)
