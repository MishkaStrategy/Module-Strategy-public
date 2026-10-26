from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from modular_core import Feature, Side

from .decimal_utils import decimal_text
from .exceptions import ProtectionTargetContractError
from .models import ProtectionTargetReason

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_MAX_DECIMAL_DIGITS = 128
_MAX_ABS_EXPONENT = 4_096
_MAX_FINGERPRINT_DEPTH = 32
_MAX_FINGERPRINT_ITEMS = 10_000
_MAX_FINGERPRINT_STRING = 65_536
_MAX_FINGERPRINT_INT_BITS = 4_096


def safe_identifier(value: object) -> str | None:
    """Return a deterministic contract-safe identifier or ``None``."""

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str | int):
        return None
    try:
        text = str(value)
    except (ValueError, TypeError):
        return None
    return text if _SAFE_ID.fullmatch(text) else None


def decimal_value(
    value: object,
    name: str,
    *,
    positive: bool = True,
    reason: ProtectionTargetReason = ProtectionTargetReason.INVALID_ENTRY_PLAN,
) -> Decimal:
    if isinstance(value, bool):
        raise ProtectionTargetContractError(f"{name} must be numeric", reason)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProtectionTargetContractError(f"{name} must be numeric", reason) from exc
    if not result.is_finite():
        raise ProtectionTargetContractError(f"{name} must be finite", reason)
    decimal_tuple = result.as_tuple()
    if len(decimal_tuple.digits) > _MAX_DECIMAL_DIGITS:
        raise ProtectionTargetContractError(
            f"{name} exceeds {_MAX_DECIMAL_DIGITS} significant digits", reason
        )
    exponent = cast(int, decimal_tuple.exponent)
    if abs(exponent) > _MAX_ABS_EXPONENT:
        raise ProtectionTargetContractError(f"{name} exponent exceeds {_MAX_ABS_EXPONENT}", reason)
    if positive and result <= 0:
        raise ProtectionTargetContractError(f"{name} must be positive", reason)
    return result


def aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProtectionTargetContractError(f"{name} must be ISO-8601 datetime") from exc
    else:
        raise ProtectionTargetContractError(f"{name} must be datetime or ISO-8601 string")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProtectionTargetContractError(f"{name} must be timezone-aware")
    return result.astimezone(UTC)


def get_path(
    mapping: Mapping[str, object],
    path: str,
    name: str,
    *,
    missing_reason: ProtectionTargetReason = ProtectionTargetReason.INVALID_ENTRY_PLAN,
) -> object:
    current: object = mapping
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise ProtectionTargetContractError(f"{name} is missing field {path!r}", missing_reason)
        current = current[segment]
    return current


def _normalize(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_FINGERPRINT_DEPTH:
        raise ProtectionTargetContractError("fingerprint nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > _MAX_FINGERPRINT_ITEMS:
            raise ProtectionTargetContractError("fingerprinted mapping is too large")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtectionTargetContractError("fingerprinted mappings require string keys")
            if len(key) > _MAX_FINGERPRINT_STRING:
                raise ProtectionTargetContractError("fingerprint key is too large")
            normalized[key] = _normalize(item, depth=depth + 1)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, list | tuple):
        if len(value) > _MAX_FINGERPRINT_ITEMS:
            raise ProtectionTargetContractError("fingerprinted sequence is too large")
        return [_normalize(item, depth=depth + 1) for item in value]
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ProtectionTargetContractError("fingerprinted datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProtectionTargetContractError("fingerprinted floats must be finite")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if value.bit_length() > _MAX_FINGERPRINT_INT_BITS:
            raise ProtectionTargetContractError("fingerprinted integer is too large")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_FINGERPRINT_STRING:
            raise ProtectionTargetContractError("fingerprinted string is too large")
        return value
    raise ProtectionTargetContractError(
        f"unsupported fingerprint value type: {type(value).__name__}"
    )


def canonical_fingerprint(payload: object) -> str:
    encoded = json.dumps(
        _normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def feature_fingerprint(feature: Feature) -> str:
    return canonical_fingerprint(
        {
            "key": feature.key,
            "source_module": feature.source_module,
            "created_at": feature.created_at,
            "valid_until": feature.valid_until,
            "schema_version": feature.schema_version,
            "feature_id": feature.feature_id,
            "symbol": feature.symbol,
            "timeframe": feature.timeframe,
            "value": feature.value,
        }
    )


@dataclass(frozen=True, slots=True)
class EntryPlanContract:
    entry_plan_id: str
    setup_id: str
    symbol: str
    timeframe: str
    side: Side
    entry_price: Decimal
    created_at: datetime
    valid_until: datetime | None
    payload: Mapping[str, object]
    source_feature_id: str | None
    source_feature_fingerprint: str


def parse_entry_plan_feature(
    feature: Feature,
    *,
    expected_symbol: str,
    expected_timeframe: str,
    source_feature_fingerprint: str | None = None,
) -> EntryPlanContract:
    if feature.schema_version not in {"1", "2"}:
        raise ProtectionTargetContractError(
            f"entry feature schema_version must be '1' or '2', got {feature.schema_version!r}"
        )
    if feature.symbol != expected_symbol:
        raise ProtectionTargetContractError(
            f"entry feature symbol {feature.symbol!r} does not match {expected_symbol!r}"
        )
    if feature.timeframe != expected_timeframe:
        raise ProtectionTargetContractError(
            f"entry feature timeframe {feature.timeframe!r} does not match {expected_timeframe!r}"
        )
    if not isinstance(feature.value, Mapping):
        raise ProtectionTargetContractError("entry feature value must be a mapping")

    payload = feature.value
    payload_schema = payload.get("schema_version", "1")
    if (
        not isinstance(payload_schema, str)
        or payload_schema not in {"1", "2"}
        or payload_schema != feature.schema_version
    ):
        raise ProtectionTargetContractError(
            "entry plan payload schema_version must match feature schema_version "
            f"{feature.schema_version!r}, got {payload_schema!r}"
        )

    plan_id_field = "entry_plan_id" if payload_schema == "1" else "plan_id"
    entry_plan_id = safe_identifier(payload.get(plan_id_field))
    setup_id = safe_identifier(payload.get("setup_id"))
    if entry_plan_id is None:
        raise ProtectionTargetContractError(
            f"{plan_id_field} is missing or contains unsupported characters"
        )
    if setup_id is None:
        raise ProtectionTargetContractError(
            "setup_id is missing or contains unsupported characters"
        )

    try:
        side = Side(str(payload.get("side", "")))
    except ValueError as exc:
        raise ProtectionTargetContractError("entry plan side must be 'buy' or 'sell'") from exc
    entry_price_field = "entry_price" if payload_schema == "1" else "price"
    entry_price = decimal_value(payload.get(entry_price_field), "entry_price")

    payload_symbol = str(payload.get("symbol", expected_symbol))
    payload_timeframe = (
        str(payload.get("timeframe", expected_timeframe))
        if payload_schema == "1"
        else expected_timeframe
    )
    if payload_symbol != expected_symbol:
        raise ProtectionTargetContractError(
            "entry plan payload symbol conflicts with feature symbol"
        )
    if payload_timeframe != expected_timeframe:
        raise ProtectionTargetContractError(
            "entry plan payload timeframe conflicts with feature timeframe"
        )

    payload_created = payload.get("created_at")
    if (
        payload_created is not None
        and aware_datetime(payload_created, "created_at") != feature.created_at
    ):
        raise ProtectionTargetContractError(
            "entry plan created_at conflicts with feature.created_at"
        )

    valid_until_field = "valid_until" if payload_schema == "1" else "expires_at"
    payload_valid_until = payload.get(valid_until_field)
    parsed_valid_until = (
        None
        if payload_valid_until is None
        else aware_datetime(payload_valid_until, valid_until_field)
    )
    if (
        parsed_valid_until is not None
        and feature.valid_until is not None
        and parsed_valid_until != feature.valid_until
    ):
        raise ProtectionTargetContractError(
            "entry plan valid_until conflicts with feature.valid_until"
        )
    valid_until = feature.valid_until or parsed_valid_until
    if valid_until is not None and valid_until < feature.created_at:
        raise ProtectionTargetContractError("entry plan valid_until cannot precede created_at")

    return EntryPlanContract(
        entry_plan_id=entry_plan_id,
        setup_id=setup_id,
        symbol=expected_symbol,
        timeframe=expected_timeframe,
        side=side,
        entry_price=entry_price,
        created_at=feature.created_at,
        valid_until=valid_until,
        payload=payload,
        source_feature_id=feature.feature_id,
        source_feature_fingerprint=(
            source_feature_fingerprint
            if source_feature_fingerprint is not None
            else feature_fingerprint(feature)
        ),
    )


def feature_process_key(feature: Feature) -> str:
    if isinstance(feature.value, Mapping):
        candidate = safe_identifier(feature.value.get("entry_plan_id"))
        if candidate is not None:
            return candidate
    feature_id = safe_identifier(feature.feature_id)
    if feature_id is not None:
        return f"feature:{feature_id}"
    return f"fingerprint:{feature_fingerprint(feature)}"


def select_structural_feature(
    features: Sequence[Feature],
    *,
    setup_id: str,
    require_setup_match: bool,
) -> Feature | None:
    exact: list[Feature] = []
    generic: list[Feature] = []
    for feature in features:
        if not isinstance(feature.value, Mapping):
            continue
        candidate = feature.value.get("setup_id")
        if candidate is None:
            generic.append(feature)
        elif safe_identifier(candidate) == setup_id:
            exact.append(feature)
    candidates = exact
    if not candidates and not require_setup_match:
        candidates = generic
    if not candidates:
        return None
    latest_time = max(feature.created_at for feature in candidates)
    latest = [feature for feature in candidates if feature.created_at == latest_time]
    fingerprints = {feature_fingerprint(feature) for feature in latest}
    if len(fingerprints) > 1:
        raise ProtectionTargetContractError(
            "ambiguous structural features at the same decision timestamp",
            ProtectionTargetReason.AMBIGUOUS_STRUCTURAL_FEATURE,
        )
    return sorted(latest, key=lambda item: item.feature_id or "")[-1]
