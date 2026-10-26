from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from modular_core import Candle, Feature, FeatureView, MarketView
from modular_strategy_trade_plan_router import (
    PendingOrderState,
    PortfolioSnapshot,
    PositionState,
    TradeSide,
)
from modular_strategy_trade_plan_router.canonical import decimal_text, stable_id
from modular_strategy_risk_request_composer import RiskRequestComposerModule
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    DatasetDescriptor,
    Direction,
    FactoryMode,
    FactoryStage,
    HoldoutReport,
    ModuleCategory,
    ModuleMaturity,
    ModuleReference,
    MarketType,
    ResourceBudget,
    RobustnessReport,
    StrategyPackage,
    StrategyArchitecture,
    StrategyAuditDecision,
    StrategyFactoryRequest,
    TimeRange,
)
from strategy_factory.runtime_binding import PartialStrategyPackageRuntimeBinding
from strategy_factory.errors import CompatibilityError
from strategy_factory.package import StrategyPackageBuilder
from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    RiskSizingModule,
)

from modular_system_acceptance.package_runtime_owner import (
    OwnerInstrumentContract,
    OwnerCurrentStateContext,
    OwnerExposureFact,
    OwnerRuntimePolicy,
    OwnerRuntimeSnapshot,
    PackageRuntimeOwnerAdapter,
    owner_instrument_version_key,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
DECISION_HASH = "b" * 64


def _package(
    directions: frozenset[Direction] = frozenset({Direction.LONG}),
) -> StrategyPackage:
    period = TimeRange(NOW - timedelta(days=100), NOW - timedelta(days=1))
    descriptor = DatasetDescriptor(
        dataset_id="dataset-synthetic",
        source="unit-test",
        symbol="BTCUSDT",
        exchange="synthetic-exchange",
        market_type=MarketType.LINEAR_FUTURES,
        base_timeframe="5m",
        available_timeframes=("5m",),
        period=period,
        timezone="UTC",
        file_format="csv",
        uri="synthetic://owner-adapter",
        sha256="e" * 64,
        row_count=100,
        received_at=NOW - timedelta(days=1),
    )
    request = StrategyFactoryRequest(
        request_id="request-synthetic",
        mode=FactoryMode.CREATE,
        symbol=descriptor.symbol,
        exchange=descriptor.exchange,
        market_type=descriptor.market_type,
        dataset=descriptor,
        allowed_timeframes=("5m",),
        initial_capital=Decimal("10000"),
        research_risk_fraction=Decimal("0.01"),
        maximum_drawdown_fraction=Decimal("0.1"),
        minimum_trade_count=20,
        objectives={"net_return": "maximize"},
        budget=ResourceBudget(
            maximum_experiments=3,
            maximum_architectures=1,
            maximum_finalists=1,
            maximum_duration_seconds=60,
            maximum_memory_bytes=1_000_000,
            maximum_cpu_workers=1,
            maximum_parallel_tasks=1,
            stage_experiment_limits={FactoryStage.STRUCTURAL_SCREENING: 3},
        ),
        random_seed=1,
        allowed_strategy_families=frozenset({"synthetic"}),
        created_at=NOW - timedelta(days=1),
    )

    def module(module_id: str, category: ModuleCategory) -> ModuleReference:
        return ModuleReference(
            module_id=module_id,
            version="1.0.0",
            category=category,
            import_path=f"synthetic:{module_id}",
            api_version="1.0",
            state_schema_version="1",
            required_timeframes=("5m",),
            required_features=(),
            produced_features=(),
            required_data=frozenset(),
            supported_markets=frozenset({MarketType.LINEAR_FUTURES}),
            supported_directions=directions,
            deterministic=True,
            test_coverage=Decimal("100"),
            maturity=ModuleMaturity.PAPER_READY,
        )

    architecture = StrategyArchitecture(
        architecture_id="architecture-synthetic",
        market_type=MarketType.LINEAR_FUTURES,
        directions=directions,
        signal=module("signal-synthetic", ModuleCategory.SIGNAL),
        context_filters=(),
        entry=module("entry-synthetic", ModuleCategory.ENTRY),
        protection=module("protection-synthetic", ModuleCategory.PROTECTION),
        target=module("target-synthetic", ModuleCategory.TARGET),
        risk=module("risk-synthetic", ModuleCategory.RISK),
    )
    periods = {
        "train": TimeRange(period.start, period.start + timedelta(days=25)),
        "validation": TimeRange(
            period.start + timedelta(days=25), period.start + timedelta(days=50)
        ),
        "test": TimeRange(period.start + timedelta(days=50), period.start + timedelta(days=75)),
        "holdout": TimeRange(period.start + timedelta(days=75), period.end),
    }
    candidate_id = "candidate-synthetic"
    candidate = CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters={},
        configuration_hash=canonical_sha256({}),
        lineage_references=("synthetic-run",),
        evaluation=CandidateEvaluation(
            evaluation_id="evaluation-synthetic",
            candidate_id=candidate_id,
            metrics={"net_return": "0.2"},
            constraints={"synthetic": True},
            pareto_front=0,
            instability_flags=(),
            complexity=Decimal("1"),
            evidence_references=("synthetic-evidence",),
        ),
    )
    evidence_time = NOW - timedelta(hours=12)
    robustness = RobustnessReport(
        report_id="robustness-synthetic",
        candidate_id=candidate_id,
        passed=True,
        scenario_results={"synthetic": "pass"},
        failure_reasons=(),
        generated_at=evidence_time,
    )
    holdout = HoldoutReport(
        report_id="holdout-synthetic",
        candidate_id=candidate_id,
        period=periods["holdout"],
        dataset_fingerprint=descriptor.fingerprint,
        metrics={"trade_count": 100},
        passed=True,
        opened_once=True,
        generated_at=evidence_time + timedelta(hours=1),
    )
    audit = StrategyAuditDecision.issue(
        audit_id="audit-synthetic",
        candidate_id=candidate_id,
        status=AdmissionStatus.APPROVED_FOR_PAPER,
        evidence_package_reference="synthetic://audit-envelope",
        evidence_hash="f" * 64,
        reason_codes=("SYNTHETIC_TEST",),
        issued_at=evidence_time + timedelta(hours=2),
        auditor_id="synthetic-auditor",
        contract_version="strategy-audit-port-v1",
        signature="synthetic-signature",
    )
    package = StrategyPackageBuilder().build(
        request=request,
        candidate=candidate,
        strategy_id="synthetic-strategy",
        version="1.0.0",
        commit_sha="d" * 40,
        research_periods=periods,
        robustness=robustness,
        holdout=holdout,
        key_results={"synthetic": True},
        known_weaknesses=(),
        disabled_regimes=(),
        evidence_links=("synthetic-evidence",),
        paper_configuration={"candidate_id": candidate_id},
        risk_policy={"research_risk_fraction": "0.01", "initial_capital": "10000"},
        execution_assumptions={
            "exchange": "synthetic-exchange",
            "market_type": MarketType.LINEAR_FUTURES.value,
        },
        audit_decision=audit,
    )
    StrategyPackageBuilder().verify(package)
    return package


def _binding(
    package: StrategyPackage,
    policy: OwnerRuntimePolicy | None = None,
    **overrides: object,
) -> PartialStrategyPackageRuntimeBinding:
    configured = policy or _policy()
    values: dict[str, object] = {
        "package_hash": package.package_hash,
        "audit_id": "audit-synthetic",
        "audit_decision_hash": DECISION_HASH,
        "modules": (
            SimpleNamespace(name="entry_model"),
            SimpleNamespace(name="protection_target"),
            RiskRequestComposerModule(
                requested_risk_fraction=configured.risk_sizing["default_risk_fraction"]
            ),
            RiskSizingModule(**dict(configured.risk_sizing)),
        ),
        "runtime_keys": (),
    }
    values.update(overrides)
    return PartialStrategyPackageRuntimeBinding(**values)  # type: ignore[arg-type]


def _policy(**overrides: object) -> OwnerRuntimePolicy:
    values: dict[str, object] = {
        "default_risk_fraction": "0.005",
        "min_risk_fraction": "0.001",
        "max_risk_fraction": "0.01",
        "max_trade_notional_multiple": "2",
        "max_portfolio_notional_multiple": "4",
        "max_symbol_notional_multiple": "2",
        "max_portfolio_risk_fraction": "0.02",
        "max_leverage": "2",
        "max_positions": 4,
        "allow_risk_reduction": False,
        "require_price_alignment": True,
        "max_processed_requests": 64,
    }
    values.update(overrides)
    return OwnerRuntimePolicy(values)


def _fixtures(*, nonempty: bool = False):
    expires = NOW + timedelta(minutes=10)
    candle = Candle(NOW, 100, 101, 99, 100, 1000, "BTCUSDT", "5m")
    entry_value = {
        "schema_version": "2",
        "plan_id": "pending",
        "module_version": "0.2.0",
        "setup_id": "setup-synthetic",
        "setup_fingerprint": "f" * 64,
        "config_fingerprint": "1" * 64,
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "limit_explicit",
        "order_type": "limit",
        "created_at": candle.close_time.isoformat(),
        "setup_confirmed_at": candle.close_time.isoformat(),
        "expires_at": expires.isoformat(),
        "ttl_bars": 12,
        "price": "100",
        "trigger_price": None,
        "limit_price": "100",
        "post_only": False,
        "rationale": "synthetic contract test",
        "metadata": {
            "strategy_id": "synthetic-strategy",
            "market": "linear_futures",
        },
    }
    identity = {
        "schema_version": "2",
        "module_version": entry_value["module_version"],
        "setup_id": entry_value["setup_id"],
        "setup_fingerprint": entry_value["setup_fingerprint"],
        "config_fingerprint": entry_value["config_fingerprint"],
        "symbol": entry_value["symbol"],
        "side": entry_value["side"],
        "entry_kind": entry_value["entry_kind"],
        "order_type": entry_value["order_type"],
        "created_at": entry_value["created_at"],
        "setup_confirmed_at": entry_value["setup_confirmed_at"],
        "expires_at": entry_value["expires_at"],
        "ttl_bars": entry_value["ttl_bars"],
        "price": decimal_text(Decimal("100")),
        "trigger_price": None,
        "limit_price": decimal_text(Decimal("100")),
        "post_only": False,
        "rationale": entry_value["rationale"],
        "metadata": entry_value["metadata"],
    }
    entry_value["plan_id"] = stable_id("entry", identity)
    entry = Feature(
        key="entry_model.plan",
        source_module="entry_model",
        created_at=candle.close_time,
        valid_until=expires,
        symbol="BTCUSDT",
        timeframe="5m",
        schema_version="2",
        feature_id="entry-synthetic",
        value=entry_value,
    )
    protection = Feature(
        key="protection_target.plan",
        source_module="protection_target",
        created_at=candle.close_time,
        valid_until=expires,
        symbol="BTCUSDT",
        timeframe="5m",
        schema_version="1",
        feature_id="protection-synthetic",
        value={
            "schema_version": "1",
            "protection_plan_id": "protection-plan-synthetic",
            "entry_plan_id": entry_value["plan_id"],
            "setup_id": "setup-synthetic",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "side": "buy",
            "entry_price": "100",
            "invalidation_price": "95",
            "stop_loss": "95",
            "take_profit": "110",
            "risk_distance": "5",
            "reward_distance": "10",
            "stop_distance_bps": "500",
            "reward_r": "2",
            "invalidation_mode": "explicit",
            "target_mode": "r_multiple",
            "created_at": candle.close_time.isoformat(),
            "valid_until": expires.isoformat(),
            "source_entry_feature_id": "entry-synthetic",
        },
    )
    view = MarketView(
        base_bars=(candle,),
        timeframe_bars={"5m": (candle,)},
        index=0,
        cash=9000.0,
        equity=10000.0,
        position=None,
        pending_orders=(),
        features=FeatureView((entry, protection), candle.close_time),
    )
    positions = (
        (
            PositionState(
                position_id="position-btc",
                symbol="ETHUSDT",
                side=TradeSide.SHORT,
                quantity=Decimal("2"),
                average_entry=Decimal("2000"),
                version="position-btc-v1",
                market="linear_futures",
                initial_risk=Decimal("30"),
                remaining_risk=Decimal("20"),
                opened_at=candle.close_time,
            ),
            PositionState(
                position_id="position-eth",
                symbol="SOLUSDT",
                side=TradeSide.SHORT,
                quantity=Decimal("100"),
                average_entry=Decimal("20"),
                version="position-eth-v1",
                market="linear_futures",
                initial_risk=Decimal("40"),
                remaining_risk=Decimal("30"),
                opened_at=candle.close_time,
            ),
        )
        if nonempty
        else ()
    )
    pending_orders = (
        (
            PendingOrderState(
                order_intent_id="order-eth",
                plan_id="plan-eth",
                symbol="SOLUSDT",
                side=TradeSide.LONG,
                order_type="limit",
                quantity=Decimal("1"),
                status="new",
                version="order-eth-v1",
                created_at=candle.close_time,
                price=Decimal("2000"),
                reserved_risk=Decimal("5"),
            ),
        )
        if nonempty
        else ()
    )
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-synthetic",
        version="portfolio-v1",
        timestamp=candle.close_time,
        equity=Decimal("10000"),
        available_balance=Decimal("9000"),
        used_margin=Decimal("1000"),
        free_margin=Decimal("9000"),
        risk_already_reserved=Decimal("5") if nonempty else Decimal("0"),
        available_risk_budget=Decimal("200"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
        positions=positions,
        pending_orders=pending_orders,
    )
    rules = InstrumentRiskRules.from_mapping(
        {
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "min_notional": "5",
            "tick_size": "0.1",
            "contract_size": "1",
            "max_leverage": "2",
        }
    )
    instrument = OwnerInstrumentContract(
        exchange="synthetic-exchange",
        symbol="BTCUSDT",
        market_type="linear_futures",
        contract_semantics="linear_quote_settled",
        quantity_unit="contracts",
        contract_size_unit="base_per_contract",
        notional_unit="USDT",
        quote_currency="USDT",
        settlement_currency="USDT",
        collateral_currency="USDT",
        pnl_currency="USDT",
        equity_currency="USDT",
        margin_currency="USDT",
        risk_budget_currency="USDT",
        instrument_version="instrument-v1",
        rules=rules,
    )
    snapshot = OwnerRuntimeSnapshot(
        as_of=candle.close_time,
        market_state_version="market-v1",
        portfolio=portfolio,
        risk=(
            PortfolioRiskSnapshot(
                gross_notional=Decimal("6000"),
                symbol_notional=Decimal("0"),
                open_risk_amount=Decimal("50"),
                open_positions=2,
            )
            if nonempty
            else PortfolioRiskSnapshot()
        ),
        portfolio_version="portfolio-v1",
        exposure_facts=(
            (
                OwnerExposureFact(
                    kind="position",
                    reference_id="position-btc",
                    exchange="synthetic-exchange",
                    symbol="ETHUSDT",
                    market_type="linear_futures",
                    side="short",
                    instrument_version="instrument-eth-v1",
                    state_version="position-btc-v1",
                    quantity=Decimal("2"),
                    quantity_unit="contracts",
                    contract_semantics="linear_quote_settled",
                    notional=Decimal("4000"),
                    notional_currency="USDT",
                    open_risk=Decimal("20"),
                    risk_currency="USDT",
                ),
                OwnerExposureFact(
                    kind="position",
                    reference_id="position-eth",
                    exchange="synthetic-exchange",
                    symbol="SOLUSDT",
                    market_type="linear_futures",
                    side="short",
                    instrument_version="instrument-sol-v1",
                    state_version="position-eth-v1",
                    quantity=Decimal("100"),
                    quantity_unit="contracts",
                    contract_semantics="linear_quote_settled",
                    notional=Decimal("2000"),
                    notional_currency="USDT",
                    open_risk=Decimal("30"),
                    risk_currency="USDT",
                ),
                OwnerExposureFact(
                    kind="pending_order",
                    reference_id="order-eth",
                    exchange="synthetic-exchange",
                    symbol="SOLUSDT",
                    market_type="linear_futures",
                    side="long",
                    instrument_version="instrument-sol-v1",
                    state_version="order-eth-v1",
                    quantity=Decimal("1"),
                    quantity_unit="contracts",
                    contract_semantics="linear_quote_settled",
                    notional=Decimal("2000"),
                    notional_currency="USDT",
                    open_risk=Decimal("5"),
                    risk_currency="USDT",
                ),
            )
            if nonempty
            else ()
        ),
    )
    current = OwnerCurrentStateContext(
        observed_at=candle.close_time,
        market_state_version="market-v1",
        instrument_versions={
            owner_instrument_version_key(
                "synthetic-exchange", "BTCUSDT", "linear_futures"
            ): "instrument-v1",
            **(
                {
                    owner_instrument_version_key(
                        "synthetic-exchange", "ETHUSDT", "linear_futures"
                    ): "instrument-eth-v1",
                    owner_instrument_version_key(
                        "synthetic-exchange", "SOLUSDT", "linear_futures"
                    ): "instrument-sol-v1",
                }
                if nonempty
                else {}
            ),
        },
        portfolio_version="portfolio-v1",
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
    )
    return view, instrument, snapshot, current


def test_prepares_real_composer_sizer_router_plan_but_keeps_execution_blocked() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    policy = _policy()
    binding = _binding(package, policy)
    bound_composer, bound_sizing = binding.modules[2:]
    prepared = PackageRuntimeOwnerAdapter().prepare(
        package=package,
        binding=binding,
        expected_package_hash=package.package_hash,
        market_view=view,
        instrument=instrument,
        snapshot=snapshot,
        current_context=current,
        policy=policy,
    )
    assert prepared.package_hash == package.package_hash
    assert prepared.portfolio_hash == snapshot.portfolio.input_hash
    assert prepared.routing.plan is not None
    assert prepared.plan_hash == prepared.routing.plan.content_hash
    assert prepared.router_input_hash
    assert prepared.owner_snapshot_hash
    assert prepared.package_binding_input_hash in prepared.routing.plan.input_hashes
    assert bound_composer.snapshot_state()["processed"]
    assert bound_sizing.snapshot_state()["counters"]["approved"] == 1

    class Boundary:
        calls = 0

        def accept(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            return object()

    boundary = Boundary()
    with pytest.raises(CompatibilityError, match="only partially bound"):
        PackageRuntimeOwnerAdapter().dispatch_if_qualified(
            prepared,
            boundary,  # type: ignore[arg-type]
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=object(),
        )
    assert boundary.calls == 0


def test_tampered_package_is_rejected_before_stateful_collaborators() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    binding = _binding(package)
    composer, sizing = binding.modules[2:]
    tampered = replace(package, symbol="ETHUSDT")
    with pytest.raises(CompatibilityError, match="hash mismatch"):
        PackageRuntimeOwnerAdapter().prepare(
            package=tampered,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )
    assert composer.snapshot_state()["processed"] == {}
    assert sizing.snapshot_state()["request_fingerprints"] == {}


def test_nonempty_multisymbol_portfolio_reconciles_owner_values() -> None:
    view, instrument, snapshot, current = _fixtures(nonempty=True)
    package = _package()
    prepared = PackageRuntimeOwnerAdapter().prepare(
        package=package,
        binding=_binding(package),
        expected_package_hash=package.package_hash,
        market_view=view,
        instrument=instrument,
        snapshot=snapshot,
        current_context=current,
        policy=_policy(),
    )
    assert prepared.routing.plan is not None
    assert prepared.portfolio_hash == snapshot.portfolio.input_hash
    assert len(snapshot.portfolio.positions) == 2
    assert len(snapshot.portfolio.pending_orders) == 1


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("missing_fact", "cover every portfolio position"),
        ("missing_order_fact", "cover every active pending order"),
        ("aggregate", "do not reconcile"),
        ("currency", "unsupported conversion"),
        ("instrument_version", "instrument version is stale"),
        ("target_instrument_version", "owner instrument version is stale"),
        ("portfolio_version", "portfolio version is stale"),
        ("market_version", "market state version is stale"),
        ("position_version", "position state version is stale"),
        ("order_version", "order state version is stale"),
        ("reservation_version", "risk reservation version is stale"),
    ],
)
def test_owner_portfolio_facts_fail_closed(mutation: str, message: str) -> None:
    view, instrument, snapshot, current = _fixtures(nonempty=True)
    package = _package()
    if mutation == "missing_fact":
        snapshot = replace(
            snapshot,
            exposure_facts=tuple(
                fact for fact in snapshot.exposure_facts if fact.reference_id != "position-btc"
            ),
        )
    elif mutation == "missing_order_fact":
        snapshot = replace(
            snapshot,
            exposure_facts=tuple(
                fact for fact in snapshot.exposure_facts if fact.kind != "pending_order"
            ),
        )
    elif mutation == "aggregate":
        snapshot = replace(
            snapshot,
            risk=replace(snapshot.risk, gross_notional=Decimal("4099")),
        )
    elif mutation == "currency":
        snapshot = replace(
            snapshot,
            exposure_facts=(
                *snapshot.exposure_facts[:-1],
                replace(snapshot.exposure_facts[-1], notional_currency="EUR"),
            ),
        )
    elif mutation == "instrument_version":
        current = replace(
            current,
            instrument_versions={
                **current.instrument_versions,
                owner_instrument_version_key(
                    "synthetic-exchange", "ETHUSDT", "linear_futures"
                ): "stale-version",
            },
        )
    elif mutation == "target_instrument_version":
        current = replace(
            current,
            instrument_versions={
                **current.instrument_versions,
                owner_instrument_version_key(
                    "synthetic-exchange", "BTCUSDT", "linear_futures"
                ): "stale-version",
            },
        )
    elif mutation == "portfolio_version":
        current = replace(current, portfolio_version="stale-portfolio")
    elif mutation == "market_version":
        current = replace(current, market_state_version="stale-market")
    elif mutation == "position_version":
        current = replace(current, position_state_version="stale-position-state")
    elif mutation == "order_version":
        current = replace(current, order_state_version="stale-order-state")
    elif mutation == "reservation_version":
        current = replace(current, risk_reservation_version="stale-reservation")
    binding = _binding(package)
    composer, sizing = binding.modules[2:]
    with pytest.raises(ValueError, match=message):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )
    assert composer.snapshot_state()["processed"] == {}
    assert sizing.snapshot_state()["request_fingerprints"] == {}


def test_market_type_direction_and_entry_identity_conflicts_reject_before_composition() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    binding = _binding(package)
    composer, sizing = binding.modules[2:]
    with pytest.raises(ValueError, match="only linear_futures"):
        replace(instrument, market_type="spot")
    with pytest.raises(ValueError, match="only linear_futures"):
        replace(instrument, market_type="inverse_futures")

    entry = view.features.latest("entry_model.plan", symbol="BTCUSDT")
    assert entry is not None
    conflicted_value = {**entry.value, "metadata": {**entry.value["metadata"], "market": "spot"}}
    conflicted_entry = replace(entry, value=conflicted_value)
    conflict_view = replace(
        view,
        features=FeatureView(
            (conflicted_entry, view.features.latest("protection_target.plan", symbol="BTCUSDT")),
            view.current.close_time,
        ),
    )
    with pytest.raises(ValueError, match="entry market identity conflicts"):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=conflict_view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )
    assert composer.snapshot_state()["processed"] == {}
    assert sizing.snapshot_state()["request_fingerprints"] == {}

    short_only_package = _package(frozenset({Direction.SHORT}))
    short_binding = _binding(short_only_package)
    short_composer, short_sizing = short_binding.modules[2:]
    with pytest.raises(ValueError, match="entry direction is not authorized"):
        PackageRuntimeOwnerAdapter().prepare(
            package=short_only_package,
            binding=short_binding,
            expected_package_hash=short_only_package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )
    assert short_composer.snapshot_state()["processed"] == {}
    assert short_sizing.snapshot_state()["request_fingerprints"] == {}


@pytest.mark.parametrize("fallback", ["instrument", "portfolio"])
def test_composer_cannot_fall_back_from_owner_features(fallback: str) -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    binding = _binding(package)
    invalid_composer = RiskRequestComposerModule(
        require_instrument_feature=fallback != "instrument",
        require_portfolio_feature=fallback != "portfolio",
    )
    binding = replace(
        binding,
        modules=(binding.modules[0], binding.modules[1], invalid_composer, binding.modules[3]),
    )
    with pytest.raises(ValueError, match="canonical owner risk features"):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )
    assert invalid_composer.snapshot_state()["processed"] == {}


def test_owner_snapshot_hash_uses_canonical_mapping_order_and_binds_versions() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    reversed_context = replace(
        current,
        instrument_versions=dict(reversed(tuple(current.instrument_versions.items()))),
    )
    adapter = PackageRuntimeOwnerAdapter()
    first = adapter.prepare(
        package=package,
        binding=_binding(package),
        expected_package_hash=package.package_hash,
        market_view=view,
        instrument=instrument,
        snapshot=snapshot,
        current_context=current,
        policy=_policy(),
    )
    second = adapter.prepare(
        package=package,
        binding=_binding(package),
        expected_package_hash=package.package_hash,
        market_view=view,
        instrument=instrument,
        snapshot=snapshot,
        current_context=reversed_context,
        policy=_policy(),
    )
    assert first.owner_snapshot_hash == second.owner_snapshot_hash
    next_snapshot = replace(snapshot, market_state_version="market-v2")
    next_context = replace(current, market_state_version="market-v2")
    changed = adapter.prepare(
        package=package,
        binding=_binding(package),
        expected_package_hash=package.package_hash,
        market_view=view,
        instrument=instrument,
        snapshot=next_snapshot,
        current_context=next_context,
        policy=_policy(),
    )
    assert first.owner_snapshot_hash != changed.owner_snapshot_hash


def test_instrument_version_keys_preserve_structured_slash_identity() -> None:
    first_identity = ("a/b", "c", "linear_futures")
    second_identity = ("a", "b/c", "linear_futures")
    assert "/".join(first_identity) == "/".join(second_identity)

    first_key = owner_instrument_version_key(*first_identity)
    second_key = owner_instrument_version_key(*second_identity)
    assert first_key != second_key
    current = OwnerCurrentStateContext(
        observed_at=NOW,
        market_state_version="market-v1",
        instrument_versions={second_key: "fresh-for-second-instrument"},
        portfolio_version="portfolio-v1",
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
    )
    assert current.instrument_versions.get(first_key) is None
    assert owner_instrument_version_key("binance", "BTC/USDT", "linear_futures")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"expected_package_hash": "2" * 64}, "package hash"),
        ({"instrument_exchange": "other-exchange"}, "instrument identity"),
        ({"portfolio_time": datetime(2026, 10, 18, 11, 0, tzinfo=UTC)}, "timestamp is stale"),
        ({"instrument_currency": "USD"}, "must match"),
    ],
)
def test_rejects_package_and_stale_or_unit_mismatches(changes, message: str) -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    expected_hash = changes.get("expected_package_hash", package.package_hash)
    if "instrument_exchange" in changes:
        instrument = replace(instrument, exchange=changes["instrument_exchange"])
    if "instrument_currency" in changes:
        with pytest.raises(ValueError, match=message):
            replace(instrument, equity_currency=changes["instrument_currency"])
        return
    if "portfolio_time" in changes:
        with pytest.raises(ValueError, match=message):
            replace(snapshot, as_of=changes["portfolio_time"])
        return
    with pytest.raises((TypeError, ValueError), match=message):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=_binding(package),
            expected_package_hash=expected_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )


def test_owner_policy_must_supply_every_risk_decision_field() -> None:
    with pytest.raises(ValueError, match="fields mismatch"):
        OwnerRuntimePolicy({"default_risk_fraction": "0.01"})


def test_unsupported_contract_semantics_fail_closed() -> None:
    _, instrument, _, _ = _fixtures()
    with pytest.raises(ValueError, match="unsupported contract semantics"):
        replace(instrument, contract_semantics="inverse")


def test_unbound_feature_source_is_not_mapped_to_package() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    binding = _binding(package)
    binding = replace(
        binding,
        modules=(*binding.modules[2:], SimpleNamespace(name="another_module")),
    )
    with pytest.raises(ValueError, match="not in the authenticated package module graph"):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=_policy(),
        )


def test_owner_policy_cannot_replace_the_bound_package_risk_config() -> None:
    view, instrument, snapshot, current = _fixtures()
    package = _package()
    owner_policy = _policy()
    binding = _binding(package)
    with pytest.raises(ValueError, match="differs from the bound package sizing config"):
        PackageRuntimeOwnerAdapter().prepare(
            package=package,
            binding=binding,
            expected_package_hash=package.package_hash,
            market_view=view,
            instrument=instrument,
            snapshot=snapshot,
            current_context=current,
            policy=OwnerRuntimePolicy(
                {**owner_policy.risk_sizing, "default_risk_fraction": Decimal("0.004")}
            ),
        )
