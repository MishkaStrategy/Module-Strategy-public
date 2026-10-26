from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from modular_core import BacktestEngine, Candle, Feature, FeatureView, MarketView
from modular_modules.entry_model import EntryKind, EntryModelModule
from modular_setup_adapter import CanonicalSetupAdapterModule
from modular_strategy_protection_target import ProtectionTargetModule
from modular_strategy_risk_request_composer import RiskRequestComposerModule
from modular_strategy_trade_plan_router import TradePlanRouterModule
from strategy_modules.breakout_return import BreakoutReturnModule
from strategy_modules.fvg_rt import (
    FEATURE_KEY as FVG_FINAL_FEATURE_KEY,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    FvgRtCompositeModule,
    FvgRtFinal15mModule,
)
from strategy_modules.liquidity_sweep import LiquiditySweepModule
from strategy_modules.risk_sizing import RiskSizingModule

from strategy_factory.adapters import ResearchRunnerAdapter, ResearchRunnerExecutionAdapter
from strategy_factory.canonical import canonical_json_bytes, canonical_sha256
from strategy_factory.catalog_v06 import TrustedRepositoryCatalogBuilder, v06_trusted_specs
from strategy_factory.contract_codec import decode_experiment_plan
from strategy_factory.contracts import (
    DatasetDescriptor,
    FactoryMode,
    FactoryStage,
    MarketType,
    ModuleCategory,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange as FactoryTimeRange,
)
from strategy_factory.coordinator import FactoryCoordinator
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.decision_log import DecisionLog
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import (
    ArchitectureGenerationHandler,
    ExperimentPlanningHandler,
    ExperimentPlanningPolicy,
    HypothesisGenerationHandler,
    RegimeDiscoveryHandler,
    StructuralScreeningHandler,
)

_FAMILIES = ("breakout_return", "liquidity_sweep", "fvg_rt")
_START = datetime(2026, 1, 5, tzinfo=UTC)
_ENTRY_KEY = "entry_model.plan"
_PROTECTION_KEY = "protection_target.plan"
_RISK_REQUEST_KEY = "risk.request"
_RISK_DECISION_KEY = "risk.decision"
_EXECUTABLE_PLAN_KEY = "trade_plan_router.executable_plan"


def _candle(
    index: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        timestamp=_START + timedelta(minutes=5 * index),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _view(current: Candle, features: Sequence[Feature] = (), *, index: int = 0) -> MarketView:
    return MarketView(
        base_bars=(current,),
        timeframe_bars={},
        index=index,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        data_gap=False,
        features=FeatureView(tuple(features), current.close_time),
    )


def _published(result: object, key: str) -> tuple[Feature, ...]:
    journal = getattr(result, "journal")
    return tuple(
        row["feature"]
        for row in journal.features
        if row["action"] == "published" and row["feature"].key == key
    )


def _one(features: Sequence[Feature], key: str, label: str) -> Feature:
    selected = tuple(feature for feature in features if feature.key == key)
    if len(selected) != 1:
        raise RuntimeError(f"{label} expected exactly one {key}, observed {len(selected)}")
    return selected[0]


def _breakout_source(*, signal_case: bool) -> tuple[Candle, tuple[Feature, ...], int, str]:
    bars = [
        _candle(0, 95.0, 100.0, 90.0, 96.0),
        _candle(1, 96.0, 99.0, 91.0, 95.0),
        _candle(2, 95.0, 98.0, 92.0, 94.0),
        _candle(3, 101.0, 104.0, 100.5, 103.0),
    ]
    if signal_case:
        bars.append(_candle(4, 99.5, 102.0, 98.5, 99.0))

    result = BacktestEngine(
        BreakoutReturnModule(
            reference_mode="rolling",
            lookback_bars=3,
            direction="above",
            breakout_confirmation="body",
            return_confirmation="body",
            min_return_depth=0.02,
            max_return_depth=0.20,
            max_return_bars=6,
        )
    ).run(bars)
    if result.journal.errors:
        raise RuntimeError("breakout_return producer emitted Core errors")
    return (
        bars[-1],
        _published(result, "breakout_return.confirmed"),
        len(bars),
        "rolling-breakout-return" if signal_case else "rolling-breakout-without-return",
    )


def _liquidity_source(*, signal_case: bool) -> tuple[Candle, tuple[Feature, ...], int, str]:
    bars = [
        _candle(0, 9.0, 10.0, 8.0, 9.0),
        _candle(1, 10.0, 12.0, 9.0, 11.0),
        _candle(2, 10.5, 11.0, 9.5, 10.0),
    ]
    if signal_case:
        bars.append(_candle(3, 11.6, 12.1, 10.8, 11.2))
    result = BacktestEngine(
        LiquiditySweepModule(
            pivot_left_bars=1,
            pivot_right_bars=1,
            min_wick_to_body_ratio=0.5,
            min_wick_fraction=0.2,
            emit_signals=True,
        )
    ).run(bars)
    if result.journal.errors:
        raise RuntimeError("liquidity_sweep producer emitted Core errors")
    return (
        bars[-1],
        _published(result, "liquidity_sweep.event"),
        len(bars),
        "confirmed-sweep" if signal_case else "confirmed-level-without-sweep",
    )


def _fvg_source(*, signal_case: bool) -> tuple[Candle, tuple[Feature, ...], int, str]:
    current = _candle(0, 100.5, 101.0, 99.5, 100.9)
    composite = FvgRtCompositeModule()
    if not signal_case:
        decision = composite.on_bar(_view(current))
        if decision.features:
            raise RuntimeError("FVG-RT no-context fixture unexpectedly emitted a snapshot")
        return current, (), 1, "incomplete-higher-timeframe-context"

    child = FvgRtFinal15mModule()
    snapshot = child._snapshot(
        "long",
        current.symbol,
        current.close_time,
        entry=100.1,
        stop=99.8,
    )
    if snapshot is None:
        raise RuntimeError("accepted FVG-RT producer rejected technical smoke snapshot")
    feature = child._feature(snapshot, current.close_time, current.symbol)
    emitted = composite._reemit_feature(child, feature)
    return current, (emitted,), 1, "owner-bound-final15-snapshot"


def _trusted_identity(family: str) -> Mapping[str, str]:
    specs = {item.spec_id: item for item in v06_trusted_specs()}
    if family not in specs:
        raise RuntimeError(f"trusted v0.6 catalog has no family {family}")
    spec = specs[family]
    instances = {
        "breakout_return": BreakoutReturnModule(),
        "liquidity_sweep": LiquiditySweepModule(),
        "fvg_rt": FvgRtCompositeModule(),
    }
    module = instances[family]
    manifest = module.manifest
    if manifest is None:
        raise RuntimeError(f"trusted family {family} has no runtime manifest")
    if (
        manifest.name != spec.expected_manifest_name
        or manifest.version != spec.expected_manifest_version
    ):
        raise RuntimeError(f"trusted family {family} manifest identity drift")
    return {
        "spec_id": spec.spec_id,
        "import_path": spec.import_path,
        "manifest_name": manifest.name,
        "manifest_version": manifest.version,
    }


def _adapter(family: str) -> CanonicalSetupAdapterModule:
    if family == "breakout_return":
        return CanonicalSetupAdapterModule()
    if family == "liquidity_sweep":
        return CanonicalSetupAdapterModule(
            sources=(
                {
                    "feature_key": "liquidity_sweep.event",
                    "profile": "liquidity_sweep_v1",
                    "source_module": "liquidity_sweep",
                },
            )
        )
    if family == "fvg_rt":
        return CanonicalSetupAdapterModule(
            sources=(
                {
                    "feature_key": FVG_FINAL_FEATURE_KEY,
                    "profile": "fvg_rt_final_15m_v1",
                    "source_module": "fvg_rt_composite",
                },
                {
                    "feature_key": FROZEN_1H_FEATURE_KEY,
                    "profile": "fvg_rt_frozen_1h_v1",
                    "source_module": "fvg_rt_composite",
                },
                {
                    "feature_key": FROZEN_4H_FEATURE_KEY,
                    "profile": "fvg_rt_frozen_4h_v1",
                    "source_module": "fvg_rt_composite",
                },
            )
        )
    raise RuntimeError(f"unsupported technical smoke family: {family}")


def _entry_model(family: str) -> EntryModelModule:
    if family == "fvg_rt":
        return EntryModelModule(
            default_entry_kind=EntryKind.LIMIT_EXPLICIT,
            ttl_bars=48,
            allowed_entry_kinds=(EntryKind.LIMIT_EXPLICIT,),
        )
    return EntryModelModule(
        default_entry_kind=EntryKind.LIMIT_RETRACEMENT,
        retracement_fraction="0.5",
        ttl_bars=12,
        allowed_entry_kinds=(EntryKind.LIMIT_RETRACEMENT,),
    )


def _protection_target(family: str) -> ProtectionTargetModule:
    if family == "fvg_rt":
        return ProtectionTargetModule(
            entry_feature_key=_ENTRY_KEY,
            invalidation_mode="entry_plan",
            invalidation_field="metadata.source_setup_metadata.invalidation_price",
            stop_buffer_bps="0",
            target_mode="structural",
            structural_target_source="entry_plan",
            target_field="metadata.source_setup_metadata.target_price",
            max_holding_seconds_field="metadata.source_setup_metadata.max_holding_seconds",
            no_progress_rule_field="metadata.source_setup_metadata.no_progress_rule",
        )
    return ProtectionTargetModule(
        entry_feature_key=_ENTRY_KEY,
        invalidation_mode="fixed_percent",
        fixed_stop_percent="0.01",
        stop_buffer_bps="0",
        target_mode="fixed_r",
        reward_r="2",
        min_stop_distance_bps="0",
        max_stop_distance_bps="9999",
    )


def _signal_source(
    family: str,
    *,
    signal_case: bool,
) -> tuple[Candle, tuple[Feature, ...], int, str]:
    if family == "breakout_return":
        return _breakout_source(signal_case=signal_case)
    if family == "liquidity_sweep":
        return _liquidity_source(signal_case=signal_case)
    if family == "fvg_rt":
        return _fvg_source(signal_case=signal_case)
    raise RuntimeError(f"unsupported technical smoke family: {family}")


def _execute_family_case(family: str, *, signal_case: bool) -> dict[str, Any]:
    current, sources, bars_processed, fixture = _signal_source(
        family,
        signal_case=signal_case,
    )
    counts = {
        "source_features": len(sources),
        "canonical_setups": 0,
        "entry_plans": 0,
        "protection_plans": 0,
        "risk_requests": 0,
        "risk_decisions": 0,
        "executable_plans": 0,
    }
    if not signal_case:
        if sources:
            raise RuntimeError(f"{family} no-signal fixture emitted a source feature")
        return {
            "fixture": fixture,
            "bars_processed": bars_processed,
            "legitimate_no_signal": True,
            "counts": counts,
        }

    if len(sources) != 1:
        raise RuntimeError(f"{family} signal fixture must emit exactly one source feature")

    adapted = _adapter(family).on_bar(_view(current, sources))
    setup = _one(adapted.features, "setup.confirmed", "Setup Adapter")
    counts["canonical_setups"] = 1

    entry_decision = _entry_model(family).on_bar(_view(current, (setup,)))
    entry = _one(entry_decision.features, _ENTRY_KEY, "Entry Model")
    counts["entry_plans"] = 1

    protection_decision = _protection_target(family).on_bar(_view(current, (entry,)))
    protection = _one(protection_decision.features, _PROTECTION_KEY, "Protection Target")
    counts["protection_plans"] = 1

    instrument_rules = Feature(
        key="instrument.rules",
        source_module="instrument_metadata",
        created_at=current.close_time,
        valid_until=current.close_time,
        symbol=current.symbol,
        timeframe=current.timeframe,
        feature_id=f"{family}-technical-smoke-instrument-rules",
        value={
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "min_notional": "5",
            "tick_size": "0.000000000000001",
            "contract_size": "1",
            "max_leverage": "20",
        },
    )
    portfolio_snapshot = Feature(
        key="portfolio.risk_snapshot",
        source_module="portfolio_snapshot",
        created_at=current.close_time,
        valid_until=current.close_time,
        symbol=current.symbol,
        timeframe=current.timeframe,
        feature_id=f"{family}-technical-smoke-portfolio-snapshot",
        value={
            "gross_notional": "0",
            "symbol_notional": "0",
            "open_risk_amount": "0",
            "open_positions": 0,
        },
    )
    composer = RiskRequestComposerModule(requested_risk_fraction="0.005")
    request_decision = composer.on_bar(
        _view(current, (entry, protection, instrument_rules, portfolio_snapshot))
    )
    risk_request = _one(request_decision.features, _RISK_REQUEST_KEY, "Risk Request Composer")
    counts["risk_requests"] = 1

    sizing = RiskSizingModule(
        default_risk_fraction="0.005",
        max_trade_notional_multiple="3",
        max_portfolio_notional_multiple="3",
        max_symbol_notional_multiple="3",
        max_leverage="3",
    )
    sizing_decision = sizing.on_bar(_view(current, (risk_request,)))
    risk_decision = _one(sizing_decision.features, _RISK_DECISION_KEY, "Risk Sizing")
    risk_payload = cast(Mapping[str, Any], risk_decision.value)
    if risk_payload.get("approved") is not True:
        raise RuntimeError(
            f"{family} technical smoke risk decision was not approved: "
            f"status={risk_payload.get('status')}, "
            f"reason={risk_payload.get('reject_reason')}, "
            f"message={risk_payload.get('message')}"
        )
    counts["risk_decisions"] = 1

    router = TradePlanRouterModule(
        long_short_policy="higher_priority",
        max_portfolio_risk_fraction="0.03",
        max_symbol_notional_fraction="3",
    )
    routed = router.on_bar(_view(current, (entry, protection, risk_decision)))
    executable = _one(routed.features, _EXECUTABLE_PLAN_KEY, "Trade Plan Router")
    counts["executable_plans"] = 1

    if any(value != 1 for value in counts.values()):
        raise RuntimeError(f"{family} signal handoff lost a stage: {counts}")

    return {
        "fixture": fixture,
        "bars_processed": bars_processed,
        "legitimate_no_signal": False,
        "counts": counts,
        "executable_plan_digest": canonical_sha256(executable.value),
    }


def _write_dataset_fixture(family: str, fixture_root: Path) -> tuple[str, str]:
    fixture_root.mkdir(parents=True, exist_ok=True)
    path = fixture_root / f"{family}.csv"
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = ["open_time,open,high,low,close,volume"]
    for index in range(64):
        timestamp = (start + timedelta(minutes=5 * index)).isoformat().replace("+00:00", "Z")
        if family == "liquidity_sweep":
            close = Decimal("100.5") if index % 2 == 0 else Decimal("99.5")
            open_price = close
            high = close + Decimal("0.5")
            low = close - Decimal("0.5")
        else:
            open_price = Decimal("100") + Decimal(index) / Decimal("10")
            high = open_price + Decimal("1")
            low = open_price - Decimal("1")
            close = open_price + Decimal("0.2")
        rows.append(
            ",".join(
                (
                    timestamp,
                    str(open_price),
                    str(high),
                    str(low),
                    str(close),
                    str(Decimal("10") + index),
                )
            )
        )
    payload = ("\n".join(rows) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return str(path), hashlib.sha256(payload).hexdigest()


class _DeterministicDatasetAnalyzer(DatasetAnalyzer):
    def analyze(self, descriptor: DatasetDescriptor, *, now: datetime | None = None):
        del now
        return super().analyze(descriptor, now=_START)


def _factory_runtime_context(
    family: str,
    commit_sha: str,
    fixture_root: Path,
) -> tuple[Any, StrategyFactoryRequest, ArchitecturePlanner]:
    specs = {item.spec_id: item for item in v06_trusted_specs()}
    adapter_ids = {
        "breakout_return": "setup_adapter_breakout_return_v1",
        "liquidity_sweep": "setup_adapter_liquidity_sweep_v1",
        "fvg_rt": "setup_adapter_fvg_rt_composite_v1",
    }
    try:
        selected_ids = (
            family,
            adapter_ids[family],
            "entry_model",
            "protection_target",
            "risk_request_composer",
            "risk_sizing",
        )
    except KeyError as exc:
        raise RuntimeError(f"unsupported technical smoke family: {family}") from exc
    try:
        selected_specs = tuple(specs[item] for item in selected_ids)
    except KeyError as exc:
        raise RuntimeError(f"trusted v0.6 planning spec missing: {exc.args[0]}") from exc

    report = TrustedRepositoryCatalogBuilder(source_commit_sha=commit_sha).build(selected_specs)
    if not report.complete:
        details = tuple(
            (issue.spec_id, issue.reason_code, issue.details) for issue in report.issues
        )
        raise RuntimeError(f"trusted v0.6 smoke catalog is incomplete: {details}")

    signal_candidates = tuple(
        item
        for item in report.registered
        if item.category is ModuleCategory.SIGNAL and item.module_id == family
    )
    if len(signal_candidates) != 1:
        raise RuntimeError(
            f"{family} technical smoke expected one trusted SIGNAL, observed "
            f"{len(signal_candidates)}"
        )

    dataset_uri, dataset_sha = _write_dataset_fixture(family, fixture_root)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    dataset = DatasetDescriptor(
        dataset_id=f"technical_smoke_{family}_v1",
        source="deterministic_synthetic_fixture",
        symbol="BTCUSDT",
        exchange="binance_usdtm",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m", "15m", "1h", "4h", "1d"),
        period=FactoryTimeRange(start, start + timedelta(days=3)),
        timezone="UTC",
        file_format="csv",
        uri=dataset_uri,
        sha256=dataset_sha,
        row_count=64,
        received_at=_START,
    )
    budget = ResourceBudget(
        maximum_experiments=1,
        maximum_architectures=8,
        maximum_finalists=1,
        maximum_duration_seconds=300,
        maximum_memory_bytes=536_870_912,
        maximum_cpu_workers=1,
        maximum_parallel_tasks=1,
        stage_experiment_limits={FactoryStage.STRUCTURAL_SCREENING: 1},
    )
    request = StrategyFactoryRequest(
        request_id=f"technical_smoke_{family}",
        mode=FactoryMode.CREATE,
        symbol=dataset.symbol,
        exchange=dataset.exchange,
        market_type=dataset.market_type,
        dataset=dataset,
        allowed_timeframes=dataset.available_timeframes,
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.005"),
        maximum_drawdown_fraction=Decimal("0.15"),
        minimum_trade_count=1,
        objectives={"profit_factor": "maximize", "max_drawdown": "minimize"},
        budget=budget,
        random_seed=17,
        allowed_strategy_families=frozenset({family}),
        created_at=_START,
    )
    return report, request, ArchitecturePlanner(StrategyGrammar(report.catalog))


def _run_research_smoke(
    family: str,
    root: Path,
    fixture_root: Path,
    commit_sha: str,
) -> dict[str, Any]:
    from modular_research_runner import (
        AtomicJsonArtifactStore,
        GitProvenance,
        LocalResearchScheduler,
        ModularBacktestEngineAdapter,
        ResearchPlanBuilder,
        ResearchRunner,
        ResumePolicy,
        RetryPolicy,
        SqliteExperimentRepository,
    )

    root.mkdir(parents=True, exist_ok=True)
    report, request, planner = _factory_runtime_context(family, commit_sha, fixture_root)
    database = root / "research.sqlite3"
    repository = SqliteExperimentRepository(database)
    expected_runtime: dict[str, str] = {}

    def run_function(job):
        expected_architecture_id = expected_runtime.get("architecture_id")
        if not expected_architecture_id:
            raise RuntimeError("Strategy Factory planning identity was not frozen before execution")
        if job.parameters.get("architecture_id") != expected_architecture_id:
            raise RuntimeError("Research Runner job architecture identity drift")
        blueprint = job.parameters.get("runtime_modules")
        if not isinstance(blueprint, (tuple, list)):
            raise RuntimeError("Research Runner job lost the trusted runtime blueprint")
        runtime_ids = {
            str(item.get("runtime_component_id")) for item in blueprint if isinstance(item, Mapping)
        }
        expected_signal_runtime_component_id = expected_runtime.get("signal_runtime_component_id")
        if not expected_signal_runtime_component_id:
            raise RuntimeError("Strategy Factory signal runtime identity was not frozen")
        if expected_signal_runtime_component_id not in runtime_ids:
            raise RuntimeError("Research Runner job signal runtime identity drift")

        signal = _execute_family_case(family, signal_case=True)
        no_signal = _execute_family_case(family, signal_case=False)
        detail = {
            "schema_version": 1,
            "family": family,
            "job_role": job.stage_role,
            "signal_case": signal,
            "no_signal_case": no_signal,
            "holdout_accessed": False,
            "promotion_attempted": False,
        }
        job.output_directory.mkdir(parents=True, exist_ok=True)
        (job.output_directory / "metrics.json").write_text(
            json.dumps(
                {
                    "profit_factor": 1.0,
                    "trade_count": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        detail_path = job.output_directory / "technical-smoke.json"
        detail_path.write_bytes(canonical_json_bytes(detail))
        return SimpleNamespace(
            run_id=f"technical-smoke-{family}-{job.run_id}",
            output_directory=job.output_directory,
            bars_processed=int(signal["bars_processed"]) + int(no_signal["bars_processed"]),
            restart_verified=True,
        )

    engine = ModularBacktestEngineAdapter(lambda job: job, run_function=run_function)
    retry = RetryPolicy.from_mapping(
        {
            "max_attempts": 2,
            "initial_backoff_seconds": 1.0,
            "backoff_multiplier": 2.0,
            "maximum_backoff_seconds": 30.0,
            "retryable_categories": ("TRANSIENT", "RESOURCE"),
        }
    )
    resume = ResumePolicy.from_mapping(
        {
            "lease_seconds": 900,
            "heartbeat_seconds": 60,
            "recover_expired_leases": True,
            "verify_success_artifacts": True,
        }
    )

    def build_runner() -> ResearchRunner:
        scheduler = LocalResearchScheduler(
            repository=repository,
            engine=engine,
            retry_policy=retry,
            resume_policy=resume,
            maximum_workers=1,
            poll_interval_seconds=0.001,
        )
        return ResearchRunner(
            repository=repository,
            artifact_store=AtomicJsonArtifactStore(root / "runner-artifacts"),
            plan_builder=ResearchPlanBuilder(root / "runner-output"),
            scheduler=scheduler,
            backtest_engine_version="technical-smoke-v1",
            metric_schema_version="technical-smoke-v1",
            module_api_version="1.0",
        )

    git = GitProvenance(
        branch="feature/1219-multi-family-technical-smoke",
        commit_sha=commit_sha,
        clean=True,
    )
    runner = build_runner()
    execution = ResearchRunnerExecutionAdapter(
        runner=runner,
        repository=repository,
        git_provenance=git,
    )
    planning_adapter = ResearchRunnerAdapter(output_root=root / "preview-output")
    handlers = {
        FactoryStage.REGIME_DISCOVERY: RegimeDiscoveryHandler(),
        FactoryStage.HYPOTHESIS_GENERATION: HypothesisGenerationHandler(report.catalog),
        FactoryStage.ARCHITECTURE_GENERATION: ArchitectureGenerationHandler(
            planner,
            external_features=frozenset({"instrument.rules", "portfolio.risk_snapshot"}),
            runtime_data=frozenset({"instrument_rules", "portfolio_snapshot"}),
        ),
        FactoryStage.EXPERIMENT_PLANNING: ExperimentPlanningHandler(
            planner=planner,
            research_adapter=planning_adapter,
            policy=ExperimentPlanningPolicy(search_method="random"),
        ),
        FactoryStage.STRUCTURAL_SCREENING: StructuralScreeningHandler(
            research_adapter=planning_adapter,
            execution_adapter=execution,
        ),
    }
    factory_state_root = root / "factory-state"
    factory_run = StrategyFactoryRun(
        request=request,
        commit_sha=commit_sha,
        state_root=factory_state_root,
        module_versions={item.module_id: item.version for item in report.registered},
        now=_START,
    )
    analyzer = _DeterministicDatasetAnalyzer(
        DatasetValidationPolicy(
            interval_seconds=300,
            minimum_rows=10,
            minimum_history_days=Decimal("0"),
        )
    )
    coordinator = FactoryCoordinator(
        run=factory_run,
        dataset_analyzer=analyzer,
        handlers=handlers,
    )

    pre_screen = tuple(coordinator.step() for _ in range(5))
    if any(item.action != "completed" for item in pre_screen):
        decisions = DecisionLog(
            factory_state_root / factory_run.run_id / "decisions.jsonl"
        ).read_all()
        failure_evidence = dict(decisions[-1].decision.evidence) if decisions else {}
        raise RuntimeError(
            f"{family} Strategy Factory pre-screening path did not complete: "
            f"{tuple((item.stage.value, item.action, item.error_type) for item in pre_screen)}; "
            f"failure_evidence={failure_evidence}"
        )
    checkpoint = factory_run.checkpoint
    if checkpoint.stage is not FactoryStage.STRUCTURAL_SCREENING:
        raise RuntimeError(
            f"{family} Strategy Factory did not reach structural screening: "
            f"{checkpoint.stage.value}"
        )

    planning_artifact = factory_run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.EXPERIMENT_PLANNING.value]
    )
    records = tuple(planning_artifact["plans"])
    if len(records) != 1:
        raise RuntimeError(
            f"{family} technical smoke expected one frozen plan, observed {len(records)}"
        )
    record = records[0]
    frozen_plan = decode_experiment_plan(record["factory_plan"])
    if frozen_plan.architecture.signal.module_id != family:
        raise RuntimeError(f"{family} Strategy Factory selected the wrong SIGNAL family")
    if frozen_plan.parameter_space.maximum_combinations != 1:
        raise RuntimeError(f"{family} Strategy Factory allocated non-bounded smoke capacity")
    expected_runtime["architecture_id"] = frozen_plan.architecture.architecture_id
    expected_runtime["signal_runtime_component_id"] = str(
        frozen_plan.architecture.signal.runtime_component_id
    )

    screened = coordinator.step()
    if screened.action != "completed":
        decisions = DecisionLog(
            factory_state_root / factory_run.run_id / "decisions.jsonl"
        ).read_all()
        failure_evidence = dict(decisions[-1].decision.evidence) if decisions else {}
        raise RuntimeError(
            f"{family} structural screening did not complete: "
            f"{screened.action}/{screened.error_type}; "
            f"failure_evidence={failure_evidence}"
        )
    checkpoint = screened.checkpoint
    if checkpoint.stage is not FactoryStage.COARSE_SEARCH:
        raise RuntimeError(
            f"{family} technical smoke crossed or missed the structural boundary: "
            f"{checkpoint.stage.value}"
        )
    structural = factory_run.load_artifact(
        checkpoint.completed_artifacts[FactoryStage.STRUCTURAL_SCREENING.value]
    )
    experiments = tuple(structural["experiments"])
    if len(experiments) != 1:
        raise RuntimeError(
            f"{family} structural smoke expected one experiment, observed {len(experiments)}"
        )
    experiment = experiments[0]
    if experiment["status"] != "completed":
        raise RuntimeError(
            f"{family} Research Runner did not reach COMPLETED: {experiment['status']}"
        )
    if dict(experiment["run_status_counts"]) != {"succeeded": 1}:
        raise RuntimeError(
            f"{family} Research Runner terminal counts drift: "
            f"{dict(experiment['run_status_counts'])}"
        )

    experiment_id = str(experiment["experiment_id"])
    runs = repository.list_runs(experiment_id)
    if len(runs) != 1 or runs[0].result is None:
        raise RuntimeError(f"{family} Research Runner result evidence is incomplete")
    detail_path = Path(runs[0].result.output_directory) / "technical-smoke.json"
    if not detail_path.is_file():
        raise RuntimeError(f"{family} technical smoke result artifact is missing")
    detail_bytes = detail_path.read_bytes()
    detail = json.loads(detail_bytes)
    detail_digest = hashlib.sha256(detail_bytes).hexdigest()

    with sqlite3.connect(database) as connection:
        grant_count = int(
            connection.execute("SELECT COUNT(*) FROM final_test_grants").fetchone()[0]
        )
    if grant_count != 0:
        raise RuntimeError("technical smoke unexpectedly created a final-test grant")

    fresh_execution = ResearchRunnerExecutionAdapter(
        runner=build_runner(),
        repository=repository,
        git_provenance=git,
    )
    resumed = fresh_execution.resume(experiment_id)
    if resumed.failure is not None or resumed.snapshot is None:
        raise RuntimeError(f"{family} fresh execution adapter could not recover terminal evidence")
    if (
        resumed.snapshot.status != "completed"
        or dict(resumed.snapshot.run_status_counts) != {"succeeded": 1}
        or tuple(resumed.snapshot.run_ids) != tuple(experiment["run_ids"])
    ):
        raise RuntimeError(f"{family} fresh execution adapter recovery evidence drift")

    restored_factory = StrategyFactoryRun.restore_existing(
        state_root=factory_state_root,
        run_id=factory_run.run_id,
    )
    if restored_factory.checkpoint.stage is not FactoryStage.COARSE_SEARCH:
        raise RuntimeError(f"{family} restored Factory checkpoint stage drift")
    restored_structural = restored_factory.load_artifact(
        restored_factory.checkpoint.completed_artifacts[FactoryStage.STRUCTURAL_SCREENING.value]
    )
    structural_digest = canonical_sha256(structural)
    if canonical_sha256(restored_structural) != structural_digest:
        raise RuntimeError(f"{family} restored structural artifact identity drift")
    structural_semantic_digest = canonical_sha256(
        {
            "factory_plan_fingerprint": canonical_sha256(frozen_plan),
            "experiment_id": experiment_id,
            "specification_fingerprint": str(experiment["specification_fingerprint"]),
            "status": str(experiment["status"]),
            "run_status_counts": dict(experiment["run_status_counts"]),
            "run_ids": tuple(experiment["run_ids"]),
        }
    )

    planning = {
        "architecture_id": frozen_plan.architecture.architecture_id,
        "architecture_fingerprint": frozen_plan.architecture.fingerprint,
        "parameter_space_id": frozen_plan.parameter_space.space_id,
        "parameter_space_fingerprint": canonical_sha256(frozen_plan.parameter_space),
        "allocated_smoke_capacity": frozen_plan.parameter_space.maximum_combinations,
        "runtime_modules": tuple(
            item.module_id for item in frozen_plan.architecture.runtime_modules
        ),
    }
    return {
        "family": family,
        "trusted_runtime": dict(_trusted_identity(family)),
        "strategy_factory_planning": planning,
        "research_specification_fingerprint": str(experiment["specification_fingerprint"]),
        "dataset_id": request.dataset.dataset_id,
        "dataset_sha256": request.dataset.sha256,
        "experiment_id": experiment_id,
        "experiment_status": str(experiment["status"]),
        "run_status_counts": dict(experiment["run_status_counts"]),
        "run_ids": tuple(experiment["run_ids"]),
        "result_artifact_sha256": detail_digest,
        "structural_semantic_sha256": structural_semantic_digest,
        "signal_case": detail["signal_case"],
        "no_signal_case": detail["no_signal_case"],
        "holdout_accessed": False,
        "final_test_grants": grant_count,
        "promotion_attempted": False,
        "allocated_smoke_capacity": frozen_plan.parameter_space.maximum_combinations,
        "execution_path": "strategy_factory_structural_screening_handler",
        "factory_checkpoint_stage": checkpoint.stage.value,
        "restart_verified": True,
    }


def build_evidence(
    *,
    root: Path,
    commit_sha: str,
    fixture_root: Path | None = None,
) -> dict[str, Any]:
    if len(commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in commit_sha
    ):
        raise ValueError("commit_sha must be a lowercase 40-character hex digest")
    resolved_fixture_root = fixture_root or (root / "fixtures")
    families = tuple(
        _run_research_smoke(
            family,
            root / family,
            resolved_fixture_root,
            commit_sha,
        )
        for family in _FAMILIES
    )
    return {
        "schema_version": 1,
        "contract": "multi-family-technical-smoke-v1",
        "candidate_sha": commit_sha,
        "families": families,
        "family_count": len(families),
        "holdout_accessed": False,
        "promotion_attempted": False,
        "governed_search_authorized": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--state-root")
    args = parser.parse_args(argv)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.state_root:
        state_root = Path(args.state_root)
        evidence = build_evidence(
            root=state_root,
            commit_sha=args.commit_sha,
            fixture_root=output.parent / "fixtures",
        )
    else:
        with tempfile.TemporaryDirectory(prefix="multi-family-technical-smoke-") as tmp:
            evidence = build_evidence(
                root=Path(tmp),
                commit_sha=args.commit_sha,
                fixture_root=output.parent / "fixtures",
            )
    output.write_bytes(canonical_json_bytes(evidence))
    print(
        json.dumps(
            {
                "status": "ok",
                "family_count": evidence["family_count"],
                "evidence_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
