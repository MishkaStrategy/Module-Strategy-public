from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

from modular_core import MarketView, Side

from .config import EntryModelConfig
from .enums import EntryKind
from .plan import EntryPlan
from .primitives import JSONValue
from .setup import ConfirmedSetup

_ROUTING_METADATA_KEYS = (
    "priority",
    "setup_priority",
    "strategy_id",
    "module_id",
    "portfolio_id",
    "market",
    "confidence",
    "incompatibility_tags",
    "exclusivity_group",
    "correlation_group",
    "reason_codes",
)


def build_entry_plan(
    *,
    config: EntryModelConfig,
    manifest_version: str,
    setup: ConfirmedSetup,
    setup_fingerprint: str,
    view: MarketView,
) -> EntryPlan:
    kind = (
        setup.entry_kind_hint
        if config.honor_setup_hint and setup.entry_kind_hint is not None
        else config.default_entry_kind
    )
    if kind not in config.allowed_entry_kind_set:
        raise ValueError(f"entry kind not allowed: {kind.value}")
    price: Decimal | None = None
    trigger: Decimal | None = None
    limit: Decimal | None = None
    rationale = (
        "setup_hint"
        if config.honor_setup_hint and setup.entry_kind_hint is not None
        else "configured_default"
    )
    if kind is EntryKind.MARKET:
        pass
    elif kind is EntryKind.LIMIT_CONFIRMATION:
        price = limit = setup.confirmation_price
    elif kind is EntryKind.LIMIT_RETRACEMENT:
        if setup.extreme_price is None:
            raise ValueError("required price missing: extreme_price")
        validate_retracement_geometry(setup)
        with localcontext() as context:
            context.prec = 80
            price = limit = setup.confirmation_price + config.retracement_fraction * (
                setup.extreme_price - setup.confirmation_price
            )
    elif kind is EntryKind.LIMIT_EXPLICIT:
        if setup.entry_price is None:
            raise ValueError("required price missing: entry_price")
        validate_limit_geometry(setup.side, setup.confirmation_price, setup.entry_price)
        price = limit = setup.entry_price
    elif kind is EntryKind.STOP_MARKET:
        trigger = setup.trigger_price or setup.entry_price
        if trigger is None:
            raise ValueError("required price missing: trigger_price")
        validate_stop_geometry(setup.side, setup.confirmation_price, trigger)
    elif kind is EntryKind.STOP_LIMIT:
        trigger = setup.trigger_price or setup.entry_price
        if trigger is None:
            raise ValueError("required price missing: trigger_price")
        validate_stop_geometry(setup.side, setup.confirmation_price, trigger)
        if setup.limit_price is not None:
            limit = setup.limit_price
        else:
            with localcontext() as context:
                context.prec = 80
                offset = config.stop_limit_offset_bps / Decimal("10000")
                multiplier = (
                    Decimal("1") + offset if setup.side is Side.BUY else Decimal("1") - offset
                )
                limit = trigger * multiplier
        price = limit
        validate_stop_limit_geometry(setup.side, trigger, limit)
    else:  # pragma: no cover
        raise ValueError(f"unsupported entry kind: {kind.value}")
    created_at = view.current.close_time
    interval = view.current.close_time - view.current.open_time
    if interval <= timedelta(0):
        raise ValueError("current timeframe has no positive duration")
    effective_ttl_bars = config.ttl_bars
    source_ttl_bars = setup.metadata.get("entry_ttl_bars")
    if source_ttl_bars is not None:
        if (
            isinstance(source_ttl_bars, bool)
            or not isinstance(source_ttl_bars, int)
            or source_ttl_bars < 1
        ):
            raise ValueError("invalid setup metadata: entry_ttl_bars must be a positive integer")
        effective_ttl_bars = min(config.ttl_bars, source_ttl_bars)
    try:
        expires_at = created_at + interval * effective_ttl_bars
    except OverflowError as exc:
        raise ValueError("entry expiry overflow") from exc
    if setup.entry_cutoff_at is not None:
        expires_at = min(expires_at, setup.entry_cutoff_at)
    if expires_at <= created_at:
        raise ValueError("entry plan expired at creation")
    metadata: dict[str, JSONValue] = {
        "retracement_fraction": str(config.retracement_fraction),
        "source_setup_metadata": dict(setup.metadata),
    }
    for key in _ROUTING_METADATA_KEYS:
        if key in setup.metadata:
            metadata[key] = setup.metadata[key]
    return EntryPlan._build_without_id(
        module_version=manifest_version,
        setup_id=setup.setup_id,
        setup_fingerprint=setup_fingerprint,
        config_fingerprint=config.fingerprint,
        symbol=setup.symbol,
        side=setup.side,
        entry_kind=kind,
        order_type=kind.order_type,
        created_at=created_at,
        setup_confirmed_at=setup.confirmed_at,
        expires_at=expires_at,
        ttl_bars=effective_ttl_bars,
        price=price,
        trigger_price=trigger,
        limit_price=limit,
        post_only=(
            config.post_only_limits
            and kind
            in {
                EntryKind.LIMIT_CONFIRMATION,
                EntryKind.LIMIT_RETRACEMENT,
                EntryKind.LIMIT_EXPLICIT,
            }
        ),
        rationale=rationale,
        metadata=metadata,
    )


def validate_retracement_geometry(setup: ConfirmedSetup) -> None:
    if setup.extreme_price is None:
        raise ValueError("required price missing: extreme_price")
    if setup.side is Side.BUY and setup.extreme_price >= setup.confirmation_price:
        raise ValueError("invalid price geometry: buy extreme must be below confirmation")
    if setup.side is Side.SELL and setup.extreme_price <= setup.confirmation_price:
        raise ValueError("invalid price geometry: sell extreme must be above confirmation")


def validate_limit_geometry(side: Side, confirmation: Decimal, limit: Decimal) -> None:
    if side is Side.BUY and limit > confirmation:
        raise ValueError("invalid price geometry: buy limit cannot be above confirmation")
    if side is Side.SELL and limit < confirmation:
        raise ValueError("invalid price geometry: sell limit cannot be below confirmation")


def validate_stop_geometry(side: Side, confirmation: Decimal, trigger: Decimal) -> None:
    if side is Side.BUY and trigger < confirmation:
        raise ValueError("invalid price geometry: buy stop trigger cannot be below confirmation")
    if side is Side.SELL and trigger > confirmation:
        raise ValueError("invalid price geometry: sell stop trigger cannot be above confirmation")


def validate_stop_limit_geometry(side: Side, trigger: Decimal, limit: Decimal) -> None:
    if side is Side.BUY and limit < trigger:
        raise ValueError("invalid price geometry: buy stop-limit price cannot be below trigger")
    if side is Side.SELL and limit > trigger:
        raise ValueError("invalid price geometry: sell stop-limit price cannot be above trigger")
