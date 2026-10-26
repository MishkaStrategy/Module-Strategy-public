from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

from modular_core import Feature, Side

from .enums import EntryKind, SetupPayloadError
from .primitives import (
    JSONValue,
    decimal_text,
    json_safe_mapping,
    optional_datetime,
    optional_decimal,
    optional_decimal_text,
    parse_datetime,
    parse_decimal,
    parse_entry_kind,
    parse_side,
    require_aware,
    require_positive_decimal,
    required_text,
    sha256_hex,
)

SUPPORTED_SETUP_SCHEMA_VERSIONS = frozenset({"1"})


@dataclass(frozen=True, slots=True)
class ConfirmedSetup:
    setup_id: str
    symbol: str
    side: Side
    confirmed_at: datetime
    confirmation_price: Decimal
    status: str = "confirmed"
    entry_kind_hint: EntryKind | None = None
    extreme_price: Decimal | None = None
    entry_price: Decimal | None = None
    trigger_price: Decimal | None = None
    limit_price: Decimal | None = None
    entry_cutoff_at: datetime | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=dict)
    source_schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.setup_id.strip():
            raise SetupPayloadError("setup_id is required")
        if not self.symbol.strip():
            raise SetupPayloadError("symbol is required")
        require_aware(self.confirmed_at, "confirmed_at")
        if self.entry_cutoff_at is not None:
            require_aware(self.entry_cutoff_at, "entry_cutoff_at")
        require_positive_decimal(self.confirmation_price, "confirmation_price")
        for name, value in (
            ("extreme_price", self.extreme_price),
            ("entry_price", self.entry_price),
            ("trigger_price", self.trigger_price),
            ("limit_price", self.limit_price),
        ):
            if value is not None:
                require_positive_decimal(value, name)
        if self.source_schema_version not in SUPPORTED_SETUP_SCHEMA_VERSIONS:
            raise SetupPayloadError(
                f"unsupported setup schema version: {self.source_schema_version}"
            )
        object.__setattr__(self, "confirmed_at", self.confirmed_at.astimezone(UTC))
        if self.entry_cutoff_at is not None:
            object.__setattr__(self, "entry_cutoff_at", self.entry_cutoff_at.astimezone(UTC))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(json_safe_mapping(self.metadata, "metadata")))
        )

    @classmethod
    def from_feature(cls, feature: Feature, *, fallback_symbol: str) -> ConfirmedSetup:
        if feature.schema_version not in SUPPORTED_SETUP_SCHEMA_VERSIONS:
            raise SetupPayloadError(f"unsupported setup schema version: {feature.schema_version}")
        return cls.from_feature_value(
            feature.value,
            feature_symbol=feature.symbol,
            fallback_symbol=fallback_symbol,
            source_schema_version=feature.schema_version,
        )

    @classmethod
    def from_feature_value(
        cls,
        value: object,
        *,
        feature_symbol: str,
        fallback_symbol: str,
        source_schema_version: str = "1",
    ) -> ConfirmedSetup:
        if not isinstance(value, Mapping):
            raise SetupPayloadError("setup feature value must be a mapping")
        allowed = {
            "setup_id",
            "status",
            "symbol",
            "side",
            "confirmed_at",
            "confirmation_price",
            "reference_price",
            "entry_kind",
            "entry_hint",
            "extreme_price",
            "entry_price",
            "trigger_price",
            "limit_price",
            "entry_cutoff_at",
            "metadata",
        }
        unknown = sorted(str(key) for key in set(value) - allowed)
        if unknown:
            raise SetupPayloadError(f"unknown setup fields: {', '.join(unknown)}")
        entry_hint_raw = value.get("entry_hint", {})
        if entry_hint_raw is None:
            entry_hint_raw = {}
        if not isinstance(entry_hint_raw, Mapping):
            raise SetupPayloadError("entry_hint must be a mapping")
        hint_allowed = {
            "kind",
            "entry_kind",
            "extreme_price",
            "entry_price",
            "trigger_price",
            "limit_price",
            "cutoff_at",
        }
        hint_unknown = sorted(str(key) for key in set(entry_hint_raw) - hint_allowed)
        if hint_unknown:
            raise SetupPayloadError(f"unknown entry_hint fields: {', '.join(hint_unknown)}")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise SetupPayloadError("metadata must be a mapping")
        hint_value = entry_hint_raw.get(
            "kind", entry_hint_raw.get("entry_kind", value.get("entry_kind"))
        )
        return cls(
            setup_id=required_text(value.get("setup_id"), "setup_id"),
            symbol=str(value.get("symbol") or feature_symbol or fallback_symbol).strip(),
            side=parse_side(value.get("side")),
            confirmed_at=parse_datetime(value.get("confirmed_at"), "confirmed_at"),
            confirmation_price=parse_decimal(
                value.get("confirmation_price", value.get("reference_price")),
                "confirmation_price",
            ),
            status=str(value.get("status", "confirmed")).strip().lower(),
            entry_kind_hint=parse_entry_kind(hint_value) if hint_value is not None else None,
            extreme_price=optional_decimal(
                entry_hint_raw.get("extreme_price", value.get("extreme_price")),
                "extreme_price",
            ),
            entry_price=optional_decimal(
                entry_hint_raw.get("entry_price", value.get("entry_price")),
                "entry_price",
            ),
            trigger_price=optional_decimal(
                entry_hint_raw.get("trigger_price", value.get("trigger_price")),
                "trigger_price",
            ),
            limit_price=optional_decimal(
                entry_hint_raw.get("limit_price", value.get("limit_price")),
                "limit_price",
            ),
            entry_cutoff_at=optional_datetime(
                entry_hint_raw.get("cutoff_at", value.get("entry_cutoff_at")),
                "entry_cutoff_at",
            ),
            metadata=json_safe_mapping(metadata, "metadata"),
            source_schema_version=source_schema_version,
        )

    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "setup_id": self.setup_id,
                "symbol": self.symbol,
                "side": self.side.value,
                "confirmed_at": self.confirmed_at.isoformat(),
                "confirmation_price": decimal_text(self.confirmation_price),
                "status": self.status,
                "entry_kind_hint": self.entry_kind_hint.value if self.entry_kind_hint else None,
                "extreme_price": optional_decimal_text(self.extreme_price),
                "entry_price": optional_decimal_text(self.entry_price),
                "trigger_price": optional_decimal_text(self.trigger_price),
                "limit_price": optional_decimal_text(self.limit_price),
                "entry_cutoff_at": (
                    self.entry_cutoff_at.isoformat() if self.entry_cutoff_at else None
                ),
                "metadata": self.metadata,
                "source_schema_version": self.source_schema_version,
            }
        )

    def fingerprint(self) -> str:
        return sha256_hex(self.canonical_payload())


def source_feature_fingerprint(feature: Feature) -> str:
    return sha256_hex(
        {
            "key": feature.key,
            "source_module": feature.source_module,
            "created_at": feature.created_at.isoformat(),
            "symbol": feature.symbol,
            "timeframe": feature.timeframe,
            "schema_version": feature.schema_version,
            "value": feature.value,
            "metadata": feature.metadata,
        }
    )
