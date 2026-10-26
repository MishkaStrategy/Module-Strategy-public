from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from modular_core import Feature, FeatureStore, MarketView
from modular_strategy_risk_request_composer import RiskRequestComposerModule
from modular_strategy_trade_plan_router import (
    DecisionNormalizer,
    DecisionRouter,
    DownstreamPlanEnvelope,
    PortfolioSnapshot,
    RouterInputPackage,
    RoutingResult,
)
from modular_trading_runner.ports import PlanExecutionBoundary
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import Direction, MarketType, StrategyPackage
from strategy_factory.package import StrategyPackageBuilder
from strategy_factory.runtime_binding import PartialStrategyPackageRuntimeBinding
from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    RiskSizingConfig,
    RiskSizingModule,
)

_RISK_CONFIG_FIELDS = frozenset(
    {
        "default_risk_fraction",
        "min_risk_fraction",
        "max_risk_fraction",
        "max_trade_notional_multiple",
        "max_portfolio_notional_multiple",
        "max_symbol_notional_multiple",
        "max_portfolio_risk_fraction",
        "max_leverage",
        "max_positions",
        "allow_risk_reduction",
        "require_price_alignment",
        "max_processed_requests",
    }
)


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-blank exact string")
    return value


def _fingerprint(value: object) -> str:
    return canonical_sha256(value)


@dataclass(frozen=True, slots=True)
class OwnerInstrumentContract:
    """Explicit owner assertion for the one supported linear risk-unit model."""

    exchange: str
    symbol: str
    market_type: str
    contract_semantics: str
    quantity_unit: str
    contract_size_unit: str
    notional_unit: str
    quote_currency: str
    settlement_currency: str
    collateral_currency: str
    pnl_currency: str
    equity_currency: str
    margin_currency: str
    risk_budget_currency: str
    instrument_version: str
    rules: InstrumentRiskRules

    def __post_init__(self) -> None:
        for name in (
            "exchange",
            "symbol",
            "market_type",
            "contract_semantics",
            "quantity_unit",
            "contract_size_unit",
            "notional_unit",
            "quote_currency",
            "settlement_currency",
            "collateral_currency",
            "pnl_currency",
            "equity_currency",
            "margin_currency",
            "risk_budget_currency",
            "instrument_version",
        ):
            _text(getattr(self, name), name)
        if self.market_type != MarketType.LINEAR_FUTURES.value:
            raise ValueError("only linear_futures market semantics are supported")
        if self.contract_semantics != "linear_quote_settled":
            raise ValueError(
                "unsupported contract semantics; only explicit linear quote settlement is supported"
            )
        if self.quantity_unit != "contracts" or self.contract_size_unit != "base_per_contract":
            raise ValueError("unsupported quantity or contract-size units")
        currencies = {
            self.quote_currency,
            self.settlement_currency,
            self.collateral_currency,
            self.pnl_currency,
            self.equity_currency,
            self.margin_currency,
            self.risk_budget_currency,
        }
        if len(currencies) != 1 or self.notional_unit != self.quote_currency:
            raise ValueError("equity, margin, risk, notional, PnL, and settlement units must match")
        if not isinstance(self.rules, InstrumentRiskRules):
            raise TypeError("rules must be InstrumentRiskRules")


@dataclass(frozen=True, slots=True)
class OwnerRuntimeSnapshot:
    """Current complete owner snapshots consumed by composer and router."""

    as_of: datetime
    market_state_version: str
    portfolio: PortfolioSnapshot
    risk: PortfolioRiskSnapshot
    portfolio_version: str
    exposure_facts: tuple[OwnerExposureFact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        _text(self.market_state_version, "market_state_version")
        _text(self.portfolio_version, "portfolio_version")
        if not isinstance(self.portfolio, PortfolioSnapshot):
            raise TypeError("portfolio must be the complete PortfolioSnapshot")
        if not isinstance(self.risk, PortfolioRiskSnapshot):
            raise TypeError("risk must be PortfolioRiskSnapshot")
        if self.portfolio.version != self.portfolio_version:
            raise ValueError("portfolio snapshot version is stale")
        if self.portfolio.timestamp != self.as_of:
            raise ValueError("portfolio snapshot timestamp is stale")
        if self.risk.open_positions != len(self.portfolio.positions):
            raise ValueError("portfolio risk facts do not cover the complete position snapshot")
        facts = tuple(self.exposure_facts)
        if not all(isinstance(item, OwnerExposureFact) for item in facts):
            raise TypeError("exposure_facts must contain OwnerExposureFact values")
        identifiers = tuple((item.kind, item.reference_id) for item in facts)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("owner exposure facts contain duplicate identities")
        object.__setattr__(
            self,
            "exposure_facts",
            tuple(sorted(facts, key=lambda item: (item.kind, item.reference_id))),
        )


@dataclass(frozen=True, slots=True)
class OwnerExposureFact:
    """Current owner-valued exposure for one portfolio position or active order."""

    kind: str
    reference_id: str
    exchange: str
    symbol: str
    market_type: str
    side: str
    instrument_version: str
    state_version: str
    quantity: Decimal
    quantity_unit: str
    contract_semantics: str
    notional: Decimal
    notional_currency: str
    open_risk: Decimal
    risk_currency: str

    def __post_init__(self) -> None:
        for name in (
            "kind",
            "reference_id",
            "exchange",
            "symbol",
            "market_type",
            "side",
            "instrument_version",
            "state_version",
            "quantity_unit",
            "contract_semantics",
            "notional_currency",
            "risk_currency",
        ):
            _text(getattr(self, name), name)
        if self.kind not in {"position", "pending_order"}:
            raise ValueError("unsupported owner exposure fact kind")
        if self.side not in {"long", "short"}:
            raise ValueError("unsupported owner exposure side")
        if self.quantity_unit != "contracts" or self.contract_semantics != "linear_quote_settled":
            raise ValueError("unsupported owner exposure quantity or contract semantics")
        for name in ("quantity", "notional", "open_risk"):
            value = Decimal(str(getattr(self, name)))
            if (
                not value.is_finite()
                or value < 0
                or (name in {"quantity", "notional"} and value == 0)
            ):
                raise ValueError(f"owner exposure {name} is invalid")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class OwnerCurrentStateContext:
    """Independent current-version tokens supplied by the owning runtime."""

    observed_at: datetime
    market_state_version: str
    instrument_versions: Mapping[str, str]
    portfolio_version: str
    position_state_version: str
    order_state_version: str
    risk_reservation_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        for name in (
            "market_state_version",
            "portfolio_version",
            "position_state_version",
            "order_state_version",
            "risk_reservation_version",
        ):
            _text(getattr(self, name), name)
        versions = dict(self.instrument_versions)
        if not versions or any(not isinstance(key, str) or not key for key in versions):
            raise ValueError("current instrument versions must be a non-empty mapping")
        for version in versions.values():
            _text(version, "instrument version")
        object.__setattr__(
            self, "instrument_versions", MappingProxyType(dict(sorted(versions.items())))
        )


def owner_instrument_version_key(exchange: str, symbol: str, market_type: str) -> str:
    """Return an unambiguous version-map key for the exact instrument identity.

    Exchange and symbol identifiers may themselves contain ``/`` or ``:``;
    callers must use this structured canonical key instead of joining fields.
    """
    for name, value in (
        ("exchange", exchange),
        ("symbol", symbol),
        ("market_type", market_type),
    ):
        _text(value, name)
    identity = {"exchange": exchange, "symbol": symbol, "market_type": market_type}
    return f"instrument:v1:{canonical_sha256(identity)}"


@dataclass(frozen=True, slots=True)
class OwnerRuntimePolicy:
    """Owner-supplied financial policy; omission/defaults are not accepted."""

    risk_sizing: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.risk_sizing, Mapping):
            raise TypeError("risk_sizing must be an explicit mapping")
        if any(not isinstance(key, str) for key in self.risk_sizing):
            raise ValueError("risk_sizing keys must be strings")
        fields = frozenset(self.risk_sizing)
        if fields != _RISK_CONFIG_FIELDS:
            missing = sorted(_RISK_CONFIG_FIELDS - fields)
            extra = sorted(fields - _RISK_CONFIG_FIELDS)
            raise ValueError(f"risk_sizing fields mismatch; missing={missing}, extra={extra}")
        if any(value is None for value in self.risk_sizing.values()):
            raise ValueError("risk_sizing fields cannot be None")
        # Use the existing config parser as the only policy validator.
        config = RiskSizingConfig.from_mapping(self.risk_sizing)
        object.__setattr__(
            self,
            "risk_sizing",
            MappingProxyType({name: getattr(config, name) for name in sorted(_RISK_CONFIG_FIELDS)}),
        )


@dataclass(frozen=True, slots=True)
class PackageRuntimePreparation:
    """Immutable compatibility result; it grants no execution qualification."""

    package_hash: str
    audit_decision_hash: str
    portfolio_hash: str
    instrument_hash: str
    owner_snapshot_hash: str
    policy_hash: str
    package_binding_input_hash: str
    router_input_hash: str
    plan_hash: str | None
    routing: RoutingResult
    binding: PartialStrategyPackageRuntimeBinding


class PackageRuntimeOwnerAdapter:
    """Compose owner facts through the existing risk and plan contracts.

    The result is preparation evidence only. The binding's qualification guard
    remains the sole gate before the existing Runner execution boundary.
    """

    def prepare(
        self,
        *,
        package: StrategyPackage,
        binding: PartialStrategyPackageRuntimeBinding,
        expected_package_hash: str,
        market_view: MarketView,
        instrument: OwnerInstrumentContract,
        snapshot: OwnerRuntimeSnapshot,
        current_context: OwnerCurrentStateContext,
        policy: OwnerRuntimePolicy,
    ) -> PackageRuntimePreparation:
        if not isinstance(package, StrategyPackage):
            raise TypeError("package must be StrategyPackage")
        if type(binding) is not PartialStrategyPackageRuntimeBinding:
            raise TypeError("binding must be PartialStrategyPackageRuntimeBinding")
        if not isinstance(market_view, MarketView):
            raise TypeError("market_view must be MarketView")
        if not isinstance(current_context, OwnerCurrentStateContext):
            raise TypeError("current_context must be OwnerCurrentStateContext")
        _digest(expected_package_hash, "expected_package_hash")
        _digest(package.package_hash, "package.package_hash")
        StrategyPackageBuilder().verify(package)
        if (
            package.package_hash != expected_package_hash
            or binding.package_hash != expected_package_hash
        ):
            raise ValueError("package hash differs from the authenticated runtime binding")
        audit_decision_hash = _digest(binding.audit_decision_hash, "audit_decision_hash")
        if binding.qualification_complete:
            raise ValueError("partial-binding adapter cannot upgrade runtime qualification")
        if instrument.exchange != package.exchange or instrument.symbol != package.symbol:
            raise ValueError("owner instrument identity differs from StrategyPackage")
        package_market = getattr(package.market_type, "value", package.market_type)
        if instrument.market_type != package_market:
            raise ValueError("owner market identity differs from StrategyPackage")
        if package.execution_assumptions.get("exchange") != instrument.exchange:
            raise ValueError("package execution exchange differs from owner instrument")
        if package.execution_assumptions.get("market_type") != instrument.market_type:
            raise ValueError("package execution market differs from owner instrument")
        if market_view.current.symbol != instrument.symbol:
            raise ValueError("current market view symbol differs from owner instrument")
        if market_view.current.close_time != snapshot.as_of:
            raise ValueError("market view is stale relative to owner snapshots")
        _validate_current_owner_facts(instrument, snapshot, current_context)
        if Decimal(str(market_view.equity)) != snapshot.portfolio.equity:
            raise ValueError("market-view equity differs from the owner portfolio snapshot")
        if Decimal(str(market_view.cash)) != snapshot.portfolio.free_margin:
            raise ValueError("market-view cash differs from owner free margin")

        instrument_hash = _fingerprint(_instrument_mapping(instrument))
        portfolio_hash = snapshot.portfolio.input_hash
        owner_snapshot_hash = _fingerprint(
            {
                "market_state_version": snapshot.market_state_version,
                "portfolio_version": snapshot.portfolio_version,
                "portfolio_hash": portfolio_hash,
                "portfolio_risk": dict(snapshot.risk.to_mapping()),
                "as_of": snapshot.as_of.isoformat(),
                "exposure_facts": snapshot.exposure_facts,
                "current_context": current_context,
            }
        )
        policy_hash = _fingerprint(policy.risk_sizing)

        composer, sizing = _bound_risk_collaborators(binding)
        if (
            not composer.require_instrument_feature
            or not composer.require_portfolio_feature
            or composer.instrument_feature_key != "instrument.rules"
            or composer.portfolio_feature_key != "portfolio.risk_snapshot"
        ):
            raise ValueError("bound composer must require the canonical owner risk features")
        owner_config = RiskSizingConfig.from_mapping(policy.risk_sizing)
        if sizing.config != owner_config:
            raise ValueError("owner risk policy differs from the bound package sizing config")
        if (
            composer.requested_risk_fraction is not None
            and composer.requested_risk_fraction != owner_config.default_risk_fraction
        ):
            raise ValueError("owner default risk differs from the bound package composer config")

        view = _publish_owner_facts(market_view, instrument.rules, snapshot.risk)
        assert view.features is not None
        normalizer = DecisionNormalizer(strict_mode=True)
        entry_feature = view.features.latest("entry_model.plan", symbol=instrument.symbol)
        protection_feature = view.features.latest(
            "protection_target.plan", symbol=instrument.symbol
        )
        if entry_feature is None or protection_feature is None:
            raise ValueError("current entry and protection plan features are required")
        bound_module_names = frozenset(getattr(module, "name", None) for module in binding.modules)
        if entry_feature.source_module not in bound_module_names:
            raise ValueError(
                "entry feature source is not in the authenticated package module graph"
            )
        if protection_feature.source_module not in bound_module_names:
            raise ValueError(
                "protection feature source is not in the authenticated package module graph"
            )
        if not isinstance(entry_feature.value, Mapping):
            raise ValueError("entry feature payload must be a mapping")
        entry_metadata = entry_feature.value.get("metadata", {})
        if not isinstance(entry_metadata, Mapping):
            raise ValueError("entry feature metadata must be a mapping")
        claimed_markets = tuple(
            value
            for value in (
                entry_feature.value.get("market"),
                entry_feature.value.get("market_type"),
                entry_metadata.get("market"),
                entry_metadata.get("market_type"),
            )
            if value is not None
        )
        if any(value != instrument.market_type for value in claimed_markets):
            raise ValueError("entry market identity conflicts with the package and owner")
        entry = normalizer.normalize_entry(
            entry_feature.value,
            feature_key=entry_feature.key,
            feature_schema_version=entry_feature.schema_version,
            feature_created_at=entry_feature.created_at,
            feature_valid_until=entry_feature.valid_until,
            feature_id=entry_feature.feature_id,
            source_module=entry_feature.source_module,
            symbol=entry_feature.symbol,
            portfolio_id=snapshot.portfolio.portfolio_id,
        )
        if entry.strategy_id != package.strategy_id:
            raise ValueError("entry strategy identity differs from StrategyPackage")
        expected_direction = {
            "long": Direction.LONG,
            "short": Direction.SHORT,
        }.get(entry.side.value)
        if expected_direction is None:
            raise ValueError("entry side cannot be mapped to a package direction")
        if expected_direction not in package.directions:
            raise ValueError("entry direction is not authorized by StrategyPackage")
        entry = replace(
            entry,
            # The owner supplies market only when the feature leaves it unspecified;
            # any explicit feature claim was checked against package+owner identity above.
            market=instrument.market_type,
            input_hash="",
            metadata={
                **dict(entry.metadata),
                "package_hash": expected_package_hash,
                "audit_decision_hash": audit_decision_hash,
                "instrument_hash": instrument_hash,
                "owner_snapshot_hash": owner_snapshot_hash,
                "risk_policy_hash": policy_hash,
            },
        )
        protection = normalizer.normalize_protection(
            protection_feature.value,
            feature_schema_version=protection_feature.schema_version,
            feature_created_at=protection_feature.created_at,
            feature_valid_until=protection_feature.valid_until,
            feature_id=protection_feature.feature_id,
            symbol=protection_feature.symbol,
        )

        # All package, owner, identity, freshness, policy, and feature checks above
        # complete before either stateful bound collaborator receives a bar.
        composed = composer.on_bar(view)
        request_feature = _one_feature(composed.features, "risk.request")
        request_store_view = _publish(view, (request_feature,), snapshot.as_of)
        sized = sizing.on_bar(request_store_view)
        decision_feature = _one_feature(sized.features, "risk.decision")
        sizing_decision = normalizer.normalize_sizing(
            decision_feature.value,
            feature_schema_version=decision_feature.schema_version,
            feature_created_at=decision_feature.created_at,
            feature_valid_until=decision_feature.valid_until,
            feature_id=decision_feature.feature_id,
            symbol=decision_feature.symbol,
        )
        router_input = RouterInputPackage(
            as_of=snapshot.as_of,
            portfolio=snapshot.portfolio,
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing_decision,),
        )
        routing = DecisionRouter().route(router_input)
        plan = routing.plan
        return PackageRuntimePreparation(
            package_hash=expected_package_hash,
            audit_decision_hash=audit_decision_hash,
            portfolio_hash=portfolio_hash,
            instrument_hash=instrument_hash,
            owner_snapshot_hash=owner_snapshot_hash,
            policy_hash=policy_hash,
            package_binding_input_hash=entry.input_hash,
            router_input_hash=router_input.correlation_id,
            plan_hash=None if plan is None else _digest(plan.content_hash, "plan.content_hash"),
            routing=routing,
            binding=binding,
        )

    def dispatch_if_qualified(
        self,
        preparation: PackageRuntimePreparation,
        boundary: PlanExecutionBoundary,
        *,
        now: datetime,
        current_index: int,
        mark_price: Decimal,
        paper_versions: object,
    ) -> object:
        """Use only the existing Runner boundary after the binding guard passes."""

        if type(preparation) is not PackageRuntimePreparation:
            raise TypeError("preparation must be PackageRuntimePreparation")
        PartialStrategyPackageRuntimeBinding.require_runtime_qualified(preparation.binding)
        plan = getattr(preparation.routing, "plan", None)
        if plan is None:
            raise ValueError("router did not prepare an executable plan")
        envelope = DownstreamPlanEnvelope.from_plan(plan)
        return boundary.accept(
            envelope,
            now=now,
            current_index=current_index,
            mark_price=mark_price,
            paper_versions=paper_versions,
        )


def _one_feature(features: tuple[Feature, ...], key: str) -> Feature:
    matches = tuple(item for item in features if item.key == key)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {key} feature")
    return matches[0]


def _bound_risk_collaborators(
    binding: PartialStrategyPackageRuntimeBinding,
) -> tuple[RiskRequestComposerModule, RiskSizingModule]:
    composers = tuple(
        module for module in binding.modules if isinstance(module, RiskRequestComposerModule)
    )
    sizings = tuple(module for module in binding.modules if isinstance(module, RiskSizingModule))
    if len(composers) != 1 or len(sizings) != 1:
        raise ValueError("package module graph must bind exactly one composer and one risk sizer")
    return composers[0], sizings[0]


def _validate_current_owner_facts(
    instrument: OwnerInstrumentContract,
    snapshot: OwnerRuntimeSnapshot,
    current: OwnerCurrentStateContext,
) -> None:
    portfolio = snapshot.portfolio
    version_pairs = (
        (current.observed_at, snapshot.as_of, "owner observation timestamp"),
        (current.market_state_version, snapshot.market_state_version, "market state"),
        (current.portfolio_version, portfolio.version, "portfolio"),
        (current.position_state_version, portfolio.position_state_version, "position state"),
        (current.order_state_version, portfolio.order_state_version, "order state"),
        (
            current.risk_reservation_version,
            portfolio.risk_reservation_version,
            "risk reservation",
        ),
    )
    for expected, actual, label in version_pairs:
        if expected != actual:
            raise ValueError(f"current owner {label} version is stale")

    target_instrument_key = owner_instrument_version_key(
        instrument.exchange, instrument.symbol, instrument.market_type
    )
    expected_instrument_keys = {target_instrument_key}
    positions = {position.position_id: position for position in portfolio.positions}
    active_orders = {
        order.order_intent_id: order for order in portfolio.pending_orders if order.active
    }
    position_facts = {
        fact.reference_id: fact for fact in snapshot.exposure_facts if fact.kind == "position"
    }
    order_facts = {
        fact.reference_id: fact for fact in snapshot.exposure_facts if fact.kind == "pending_order"
    }
    if set(position_facts) != set(positions):
        raise ValueError("owner exposure facts must cover every portfolio position exactly")
    if set(order_facts) != set(active_orders):
        raise ValueError("owner exposure facts must cover every active pending order exactly")

    all_facts = tuple(snapshot.exposure_facts)
    expected_instrument_keys.update(
        owner_instrument_version_key(fact.exchange, fact.symbol, fact.market_type)
        for fact in all_facts
    )
    if not expected_instrument_keys <= set(current.instrument_versions):
        raise ValueError("current owner context lacks instrument versions for portfolio facts")
    if current.instrument_versions[target_instrument_key] != instrument.instrument_version:
        raise ValueError("owner instrument version is stale")

    for fact in all_facts:
        if fact.market_type != MarketType.LINEAR_FUTURES.value:
            raise ValueError("portfolio contains unsupported market semantics")
        if (
            fact.notional_currency != instrument.notional_unit
            or fact.risk_currency != instrument.risk_budget_currency
        ):
            raise ValueError("portfolio exposure currency requires unsupported conversion")
        key = owner_instrument_version_key(fact.exchange, fact.symbol, fact.market_type)
        if current.instrument_versions[key] != fact.instrument_version:
            raise ValueError("owner portfolio instrument version is stale")

    gross_notional = Decimal("0")
    target_symbol_notional = Decimal("0")
    open_risk = Decimal("0")
    for position_id, position in positions.items():
        fact = position_facts[position_id]
        if (
            fact.quantity != position.quantity
            or fact.state_version != position.version
            or fact.symbol != position.symbol
            or fact.side != position.side.value
            or position.market != fact.market_type
            or fact.open_risk != position.remaining_risk
        ):
            raise ValueError("owner position facts do not match the complete portfolio position")
        gross_notional += fact.notional
        if fact.symbol == instrument.symbol:
            target_symbol_notional += fact.notional
        open_risk += fact.open_risk

    if (
        gross_notional != snapshot.risk.gross_notional
        or target_symbol_notional != snapshot.risk.symbol_notional
        or open_risk != snapshot.risk.open_risk_amount
    ):
        raise ValueError("owner position valuations do not reconcile to portfolio risk aggregates")

    pending_reserved_risk = Decimal("0")
    for order_id, order in active_orders.items():
        fact = order_facts[order_id]
        if (
            fact.quantity != order.quantity
            or fact.state_version != order.version
            or fact.symbol != order.symbol
            or fact.side != order.side.value
        ):
            raise ValueError("owner pending-order facts do not match the complete portfolio order")
        if fact.open_risk != order.reserved_risk:
            raise ValueError("owner pending-order risk differs from its reserved risk")
        pending_reserved_risk += fact.open_risk
    if pending_reserved_risk != portfolio.risk_already_reserved:
        raise ValueError(
            "owner pending-order reservations do not reconcile to portfolio reservations"
        )


def _instrument_mapping(instrument: OwnerInstrumentContract) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "exchange": instrument.exchange,
            "symbol": instrument.symbol,
            "market_type": instrument.market_type,
            "contract_semantics": instrument.contract_semantics,
            "quantity_unit": instrument.quantity_unit,
            "contract_size_unit": instrument.contract_size_unit,
            "notional_unit": instrument.notional_unit,
            "quote_currency": instrument.quote_currency,
            "settlement_currency": instrument.settlement_currency,
            "collateral_currency": instrument.collateral_currency,
            "pnl_currency": instrument.pnl_currency,
            "equity_currency": instrument.equity_currency,
            "margin_currency": instrument.margin_currency,
            "risk_budget_currency": instrument.risk_budget_currency,
            "instrument_version": instrument.instrument_version,
            "rules": dict(instrument.rules.to_mapping()),
        }
    )


def _publish_owner_facts(
    market_view: MarketView,
    rules: InstrumentRiskRules,
    risk: PortfolioRiskSnapshot,
) -> MarketView:
    current = market_view.current
    base = FeatureStore()
    if market_view.features is not None:
        for key in market_view.features.keys():
            if key in {"instrument.rules", "portfolio.risk_snapshot"}:
                continue
            for feature in market_view.features.get_all(key):
                base.publish(feature)
    base.publish(
        Feature(
            key="instrument.rules",
            source_module="system_acceptance.owner",
            created_at=current.close_time,
            value=rules.to_mapping(),
            symbol=current.symbol,
            timeframe=current.timeframe,
        )
    )
    base.publish(
        Feature(
            key="portfolio.risk_snapshot",
            source_module="system_acceptance.owner",
            created_at=current.close_time,
            value=risk.to_mapping(),
            symbol=current.symbol,
            timeframe=current.timeframe,
        )
    )
    return replace(market_view, features=base.snapshot(current.close_time))


def _publish(
    market_view: MarketView, features: tuple[Feature, ...], timestamp: datetime
) -> MarketView:
    store = FeatureStore()
    if market_view.features is not None:
        for key in market_view.features.keys():
            for feature in market_view.features.get_all(key):
                store.publish(feature)
    for feature in features:
        store.publish(feature)
    return replace(market_view, features=store.snapshot(timestamp))


__all__ = [
    "OwnerCurrentStateContext",
    "OwnerExposureFact",
    "OwnerInstrumentContract",
    "OwnerRuntimePolicy",
    "OwnerRuntimeSnapshot",
    "PackageRuntimeOwnerAdapter",
    "PackageRuntimePreparation",
    "owner_instrument_version_key",
]
