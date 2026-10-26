from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

from modular_core import Feature, MarketView, Side

from .primitives import aware_datetime, decimal_text, decimal_value, primitive, required_text

CANONICAL_SETUP_SCHEMA_VERSION = "1"

_ALLOWED_CANONICAL_FIELDS = frozenset(
    {
        "setup_id",
        "status",
        "symbol",
        "side",
        "confirmed_at",
        "confirmation_price",
        "entry_kind",
        "extreme_price",
        "entry_price",
        "trigger_price",
        "limit_price",
        "entry_cutoff_at",
        "metadata",
    }
)


def build_canonical_setup_payload(
    *,
    setup_id: str,
    symbol: str,
    side: Side,
    confirmed_at: datetime,
    confirmation_price: Decimal,
    metadata: Mapping[str, object],
    entry_kind: str | None = None,
    extreme_price: Decimal | None = None,
    entry_price: Decimal | None = None,
    trigger_price: Decimal | None = None,
    limit_price: Decimal | None = None,
    entry_cutoff_at: datetime | None = None,
) -> Mapping[str, object]:
    """Build canonical setup schema 1 without adding trading policy."""

    canonical_setup_id = required_text(setup_id, "setup_id")
    canonical_symbol = required_text(symbol, "symbol")
    if not isinstance(side, Side):
        raise ValueError("side must be buy or sell")
    canonical_confirmed_at = aware_datetime(confirmed_at, "confirmed_at")
    canonical_confirmation = decimal_value(confirmation_price, "confirmation_price")

    normalized_metadata = primitive(metadata, "metadata")
    if not isinstance(normalized_metadata, Mapping):
        raise ValueError("metadata must be a mapping")

    payload: dict[str, object] = {
        "setup_id": canonical_setup_id,
        "status": "confirmed",
        "symbol": canonical_symbol,
        "side": side.value,
        "confirmed_at": canonical_confirmed_at.astimezone(UTC).isoformat(),
        "confirmation_price": decimal_text(canonical_confirmation),
    }

    if entry_kind is not None:
        payload["entry_kind"] = required_text(entry_kind, "entry_kind")
    for name, value in (
        ("extreme_price", extreme_price),
        ("entry_price", entry_price),
        ("trigger_price", trigger_price),
        ("limit_price", limit_price),
    ):
        if value is not None:
            payload[name] = decimal_text(decimal_value(value, name))
    if entry_cutoff_at is not None:
        payload["entry_cutoff_at"] = aware_datetime(entry_cutoff_at, "entry_cutoff_at").isoformat()
    payload["metadata"] = dict(normalized_metadata)
    return MappingProxyType(payload)


def validate_canonical_setup_payload(
    payload: Mapping[str, object],
    *,
    feature: Feature,
    view: MarketView,
) -> None:
    """Fail closed if a profile emits a payload Entry Model cannot trust."""

    unknown = sorted(str(key) for key in set(payload) - _ALLOWED_CANONICAL_FIELDS)
    if unknown:
        raise ValueError(f"unknown canonical setup fields: {', '.join(unknown)}")

    if required_text(payload.get("status"), "status").lower() != "confirmed":
        raise ValueError("canonical setup status must be confirmed")

    symbol = required_text(payload.get("symbol"), "symbol")
    if symbol != view.current.symbol or feature.symbol not in {view.current.symbol, "*"}:
        raise ValueError(
            "symbol mismatch between canonical setup, source feature and current stream"
        )

    side_text = required_text(payload.get("side"), "side").lower()
    try:
        Side(side_text)
    except ValueError as exc:
        raise ValueError("side must be buy or sell") from exc

    confirmed_at = aware_datetime(payload.get("confirmed_at"), "confirmed_at")
    if confirmed_at != feature.created_at or feature.created_at != view.current.close_time:
        raise ValueError(
            "feature causality violation: canonical confirmation and publication must be same-bar"
        )

    required_text(payload.get("setup_id"), "setup_id")
    decimal_value(payload.get("confirmation_price"), "confirmation_price")

    if "entry_kind" in payload:
        required_text(payload.get("entry_kind"), "entry_kind")
    for name in ("extreme_price", "entry_price", "trigger_price", "limit_price"):
        if payload.get(name) is not None:
            decimal_value(payload.get(name), name)
    if payload.get("entry_cutoff_at") is not None:
        aware_datetime(payload.get("entry_cutoff_at"), "entry_cutoff_at")

    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    primitive(metadata, "metadata")
