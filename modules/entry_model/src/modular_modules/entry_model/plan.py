from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

from modular_core import OrderType, Side

from .enums import EntryKind
from .primitives import (
    JSONValue,
    json_safe_mapping,
    optional_decimal,
    optional_decimal_text,
    parse_datetime,
    parse_entry_kind,
    parse_side,
    primitive_json,
    require_aware,
    require_positive_decimal,
    required_text,
    sha256_hex,
)

PLAN_SCHEMA_VERSION = "2"
LEGACY_PLAN_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class EntryPlan:
    plan_id: str
    module_version: str
    setup_id: str
    setup_fingerprint: str
    config_fingerprint: str
    symbol: str
    side: Side
    entry_kind: EntryKind
    order_type: OrderType
    created_at: datetime
    setup_confirmed_at: datetime
    expires_at: datetime
    ttl_bars: int
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    limit_price: Decimal | None = None
    post_only: bool = False
    rationale: str = ""
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    schema_version: str = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, text_value in (
            ("plan_id", self.plan_id),
            ("module_version", self.module_version),
            ("setup_id", self.setup_id),
            ("setup_fingerprint", self.setup_fingerprint),
            ("config_fingerprint", self.config_fingerprint),
            ("symbol", self.symbol),
        ):
            if not text_value:
                raise ValueError(f"{name} is required")
        if self.schema_version != PLAN_SCHEMA_VERSION:
            raise ValueError(f"unsupported EntryPlan schema: {self.schema_version}")
        if len(self.setup_fingerprint) != 64 or len(self.config_fingerprint) != 64:
            raise ValueError("setup/config fingerprints must be SHA-256 hex strings")
        require_aware(self.created_at, "created_at")
        require_aware(self.setup_confirmed_at, "setup_confirmed_at")
        require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if (
            isinstance(self.ttl_bars, bool)
            or not isinstance(self.ttl_bars, int)
            or self.ttl_bars < 1
        ):
            raise ValueError("ttl_bars must be a positive integer")
        if not isinstance(self.post_only, bool):
            raise ValueError("post_only must be boolean")
        for name, price_value in (
            ("price", self.price),
            ("trigger_price", self.trigger_price),
            ("limit_price", self.limit_price),
        ):
            if price_value is not None:
                require_positive_decimal(price_value, name)
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "setup_confirmed_at", self.setup_confirmed_at.astimezone(UTC))
        object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(json_safe_mapping(self.metadata, "metadata")))
        )
        expected = deterministic_plan_id(self.identity_payload())
        if self.plan_id != expected:
            raise ValueError("plan_id does not match canonical plan identity")
        self._validate_order_fields()
        if self.entry_kind.order_type is not self.order_type:
            raise ValueError("entry_kind and order_type disagree")

    def _validate_order_fields(self) -> None:
        if self.order_type is OrderType.MARKET:
            if any(
                value is not None for value in (self.price, self.trigger_price, self.limit_price)
            ):
                raise ValueError("market entry must not contain prices")
            if self.post_only:
                raise ValueError("market entry cannot be post_only")
        elif self.order_type is OrderType.LIMIT:
            if (
                self.price is None
                or self.limit_price != self.price
                or self.trigger_price is not None
            ):
                raise ValueError("limit entry requires matching price and limit_price only")
        elif self.order_type is OrderType.STOP_MARKET:
            if self.trigger_price is None or self.price is not None or self.limit_price is not None:
                raise ValueError("stop-market entry requires trigger_price only")
            if self.post_only:
                raise ValueError("stop-market entry cannot be post_only")
        elif self.order_type is OrderType.STOP_LIMIT:
            if (
                self.trigger_price is None
                or self.limit_price is None
                or self.price != self.limit_price
            ):
                raise ValueError("stop-limit entry requires trigger and matching limit/price")
            if self.post_only:
                raise ValueError("stop-limit entry cannot be post_only")
        else:
            raise ValueError(f"unsupported entry order type: {self.order_type.value}")

    def identity_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "module_version": self.module_version,
                "setup_id": self.setup_id,
                "setup_fingerprint": self.setup_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "symbol": self.symbol,
                "side": self.side.value,
                "entry_kind": self.entry_kind.value,
                "order_type": self.order_type.value,
                "created_at": self.created_at.astimezone(UTC).isoformat(),
                "setup_confirmed_at": self.setup_confirmed_at.astimezone(UTC).isoformat(),
                "expires_at": self.expires_at.astimezone(UTC).isoformat(),
                "ttl_bars": self.ttl_bars,
                "price": optional_decimal_text(self.price),
                "trigger_price": optional_decimal_text(self.trigger_price),
                "limit_price": optional_decimal_text(self.limit_price),
                "post_only": self.post_only,
                "rationale": self.rationale,
                "metadata": self.metadata,
            }
        )

    def to_feature_value(self) -> Mapping[str, object]:
        payload = {"plan_id": self.plan_id, **dict(self.identity_payload())}
        primitive = primitive_json(payload)
        if not isinstance(primitive, dict):  # pragma: no cover - construction invariant
            raise ValueError("entry plan serialization did not produce a mapping")
        return primitive

    @classmethod
    def from_feature_value(cls, value: object) -> EntryPlan:
        if not isinstance(value, Mapping):
            raise ValueError("entry plan feature value must be a mapping")
        schema = str(value.get("schema_version", LEGACY_PLAN_SCHEMA_VERSION))
        if schema == LEGACY_PLAN_SCHEMA_VERSION:
            return cls._from_legacy(value)
        if schema != PLAN_SCHEMA_VERSION:
            raise ValueError(f"unsupported EntryPlan schema: {schema}")
        allowed = {
            "plan_id",
            "schema_version",
            "module_version",
            "setup_id",
            "setup_fingerprint",
            "config_fingerprint",
            "symbol",
            "side",
            "entry_kind",
            "order_type",
            "created_at",
            "setup_confirmed_at",
            "expires_at",
            "ttl_bars",
            "price",
            "trigger_price",
            "limit_price",
            "post_only",
            "rationale",
            "metadata",
        }
        unknown = sorted(str(key) for key in set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown EntryPlan fields: {', '.join(unknown)}")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("entry plan metadata must be a mapping")
        ttl = value.get("ttl_bars")
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ValueError("entry plan ttl_bars must be integer")
        post_only = value.get("post_only", False)
        if not isinstance(post_only, bool):
            raise ValueError("entry plan post_only must be boolean")
        try:
            order_type = OrderType(str(value.get("order_type", "")).strip().lower())
        except ValueError as exc:
            raise ValueError("invalid entry plan order_type") from exc
        return cls(
            plan_id=required_text(value.get("plan_id"), "plan_id"),
            module_version=required_text(value.get("module_version"), "module_version"),
            setup_id=required_text(value.get("setup_id"), "setup_id"),
            setup_fingerprint=required_text(value.get("setup_fingerprint"), "setup_fingerprint"),
            config_fingerprint=required_text(value.get("config_fingerprint"), "config_fingerprint"),
            symbol=required_text(value.get("symbol"), "symbol"),
            side=parse_side(value.get("side")),
            entry_kind=parse_entry_kind(value.get("entry_kind")),
            order_type=order_type,
            created_at=parse_datetime(value.get("created_at"), "created_at"),
            setup_confirmed_at=parse_datetime(
                value.get("setup_confirmed_at"), "setup_confirmed_at"
            ),
            expires_at=parse_datetime(value.get("expires_at"), "expires_at"),
            ttl_bars=ttl,
            price=optional_decimal(value.get("price"), "price"),
            trigger_price=optional_decimal(value.get("trigger_price"), "trigger_price"),
            limit_price=optional_decimal(value.get("limit_price"), "limit_price"),
            post_only=post_only,
            rationale=str(value.get("rationale", "")),
            metadata=json_safe_mapping(metadata, "metadata"),
            schema_version=schema,
        )

    @classmethod
    def _from_legacy(cls, value: Mapping[object, object]) -> EntryPlan:
        # Legacy schema is readable but promoted to schema 2 with explicit legacy fingerprints.
        metadata_raw = value.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise ValueError("legacy entry plan metadata must be a mapping")
        ttl = value.get("ttl_bars")
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ValueError("legacy entry plan ttl_bars must be integer")
        post_only = value.get("post_only", False)
        if not isinstance(post_only, bool):
            raise ValueError("legacy entry plan post_only must be boolean")
        entry_kind = parse_entry_kind(value.get("entry_kind"))
        order_type_raw = str(value.get("order_type", "")).strip().lower()
        try:
            order_type = OrderType(order_type_raw)
        except ValueError as exc:
            raise ValueError(f"{order_type_raw} is not a valid OrderType") from exc
        created_at = parse_datetime(value.get("created_at"), "created_at")
        setup_confirmed_at = parse_datetime(value.get("setup_confirmed_at"), "setup_confirmed_at")
        expires_at = parse_datetime(value.get("expires_at"), "expires_at")
        setup_id = required_text(value.get("setup_id"), "setup_id")
        symbol = required_text(value.get("symbol"), "symbol")
        legacy_source = {
            "legacy_plan_id": value.get("plan_id"),
            "setup_id": setup_id,
            "symbol": symbol,
            "side": str(value.get("side")),
            "entry_kind": entry_kind.value,
            "order_type": order_type.value,
            "created_at": created_at.isoformat(),
            "setup_confirmed_at": setup_confirmed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "ttl_bars": ttl,
            "price": value.get("price"),
            "trigger_price": value.get("trigger_price"),
            "limit_price": value.get("limit_price"),
            "post_only": post_only,
            "rationale": value.get("rationale", ""),
            "metadata": metadata_raw,
        }
        setup_fp = sha256_hex({"legacy_setup": setup_id, "source": legacy_source})
        config_fp = sha256_hex({"legacy_config": True})
        legacy_metadata = {
            "legacy_plan_id": (None if value.get("plan_id") is None else str(value.get("plan_id"))),
            **dict(json_safe_mapping(metadata_raw, "legacy_metadata")),
        }
        partial = cls._build_without_id(
            module_version="0.1.0-legacy",
            setup_id=setup_id,
            setup_fingerprint=setup_fp,
            config_fingerprint=config_fp,
            symbol=symbol,
            side=parse_side(value.get("side")),
            entry_kind=entry_kind,
            order_type=order_type,
            created_at=created_at,
            setup_confirmed_at=setup_confirmed_at,
            expires_at=expires_at,
            ttl_bars=ttl,
            price=optional_decimal(value.get("price"), "price"),
            trigger_price=optional_decimal(value.get("trigger_price"), "trigger_price"),
            limit_price=optional_decimal(value.get("limit_price"), "limit_price"),
            post_only=post_only,
            rationale=str(value.get("rationale", "legacy")),
            metadata=legacy_metadata,
        )
        return partial

    @classmethod
    def _build_without_id(
        cls,
        *,
        module_version: str,
        setup_id: str,
        setup_fingerprint: str,
        config_fingerprint: str,
        symbol: str,
        side: Side,
        entry_kind: EntryKind,
        order_type: OrderType,
        created_at: datetime,
        setup_confirmed_at: datetime,
        expires_at: datetime,
        ttl_bars: int,
        price: Decimal | None,
        trigger_price: Decimal | None,
        limit_price: Decimal | None,
        post_only: bool,
        rationale: str,
        metadata: Mapping[str, JSONValue],
    ) -> EntryPlan:
        identity_payload: dict[str, object] = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "module_version": module_version,
            "setup_id": setup_id,
            "setup_fingerprint": setup_fingerprint,
            "config_fingerprint": config_fingerprint,
            "symbol": symbol,
            "side": side.value,
            "entry_kind": entry_kind.value,
            "order_type": order_type.value,
            "created_at": created_at.astimezone(UTC).isoformat(),
            "setup_confirmed_at": setup_confirmed_at.astimezone(UTC).isoformat(),
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "ttl_bars": ttl_bars,
            "price": optional_decimal_text(price),
            "trigger_price": optional_decimal_text(trigger_price),
            "limit_price": optional_decimal_text(limit_price),
            "post_only": post_only,
            "rationale": rationale,
            "metadata": metadata,
        }
        return cls(
            plan_id=deterministic_plan_id(identity_payload),
            module_version=module_version,
            setup_id=setup_id,
            setup_fingerprint=setup_fingerprint,
            config_fingerprint=config_fingerprint,
            symbol=symbol,
            side=side,
            entry_kind=entry_kind,
            order_type=order_type,
            created_at=created_at,
            setup_confirmed_at=setup_confirmed_at,
            expires_at=expires_at,
            ttl_bars=ttl_bars,
            price=price,
            trigger_price=trigger_price,
            limit_price=limit_price,
            post_only=post_only,
            rationale=rationale,
            metadata=metadata,
            schema_version=PLAN_SCHEMA_VERSION,
        )


def deterministic_plan_id(identity_payload: Mapping[str, object]) -> str:
    return "entry-" + sha256_hex(identity_payload)[:32]
