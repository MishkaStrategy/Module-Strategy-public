from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .exceptions import ProtectionTargetStateError

_STATE_VERSION = 2
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_STATE_IDENTITY = 512
_MAX_STATE_TIMESTAMP = 128
_MAX_STATE_NUMERIC = 1_024
_MAX_MODULE_VERSION = 64
_MAX_COUNTER = 2**63 - 1


@dataclass(frozen=True, slots=True)
class Counters:
    bars_seen: int = 0
    gap_bars: int = 0
    accepted_plans: int = 0
    rejected_plans: int = 0
    duplicate_bars: int = 0
    duplicate_plans: int = 0
    missing_entry_bars: int = 0

    def increment(self, **changes: int) -> Counters:
        values = dict(self.as_mapping())
        for key, amount in changes.items():
            if key not in values:
                raise KeyError(key)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("counter increments must be non-negative integers")
            values[key] += amount
            if values[key] > _MAX_COUNTER:
                raise OverflowError(f"counter {key} exceeds signed 64-bit range")
        return Counters(**values)

    def as_mapping(self) -> Mapping[str, int]:
        return {
            "bars_seen": self.bars_seen,
            "gap_bars": self.gap_bars,
            "accepted_plans": self.accepted_plans,
            "rejected_plans": self.rejected_plans,
            "duplicate_bars": self.duplicate_bars,
            "duplicate_plans": self.duplicate_plans,
            "missing_entry_bars": self.missing_entry_bars,
        }


@dataclass(frozen=True, slots=True)
class RestoredState:
    bound_symbol: str | None
    bound_timeframe: str | None
    last_bar_time: datetime | None
    last_bar_signature: tuple[str, ...] | None
    last_event_fingerprint: str | None
    processed: OrderedDict[str, str]
    counters: Counters
    migrated_from_v1: bool = False


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_payload(
    *,
    module_version: str,
    config_fingerprint: str,
    bound_symbol: str | None,
    bound_timeframe: str | None,
    last_bar_time: datetime | None,
    last_bar_signature: tuple[str, ...] | None,
    last_event_fingerprint: str | None,
    processed: OrderedDict[str, str],
    counters: Counters,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "state_version": _STATE_VERSION,
        "module_version": module_version,
        "config_fingerprint": config_fingerprint,
        "bound_symbol": bound_symbol,
        "bound_timeframe": bound_timeframe,
        "last_bar_time": last_bar_time.isoformat() if last_bar_time is not None else None,
        "last_bar_signature": list(last_bar_signature) if last_bar_signature is not None else None,
        "last_event_fingerprint": last_event_fingerprint,
        "processed": [
            {"process_key": process_key, "feature_fingerprint": fingerprint}
            for process_key, fingerprint in processed.items()
        ],
        "counters": dict(counters.as_mapping()),
    }
    payload["state_digest"] = _digest(payload)
    return payload


def parse_snapshot(
    state: Mapping[str, object],
    *,
    module_version: str,
    config_fingerprint: str,
    legacy_config_fingerprint: str,
    compatible_module_versions: tuple[str, ...] = (),
    max_processed_plans: int,
) -> RestoredState:
    if not isinstance(state, Mapping):
        raise ProtectionTargetStateError("state must be a mapping")
    if any(not isinstance(key, str) for key in state):
        raise ProtectionTargetStateError("state keys must be strings")
    state_version = state.get("state_version")
    if (
        isinstance(state_version, bool)
        or not isinstance(state_version, int)
        or state_version not in {1, _STATE_VERSION}
    ):
        raise ProtectionTargetStateError("unsupported state_version")
    migrated = state_version == 1
    allowed = {
        "state_version",
        "module_version",
        "config_fingerprint",
        "bound_symbol",
        "bound_timeframe",
        "last_bar_time",
        "last_bar_signature",
        "processed",
        "counters",
    }
    if not migrated:
        allowed.update({"last_event_fingerprint", "state_digest"})
    unknown = sorted(set(state) - allowed)
    if unknown:
        raise ProtectionTargetStateError(f"unknown state keys: {', '.join(unknown)}")
    raw_module_version = state.get("module_version")
    if (
        not isinstance(raw_module_version, str)
        or not raw_module_version
        or len(raw_module_version) > _MAX_MODULE_VERSION
    ):
        raise ProtectionTargetStateError("module_version must be a bounded non-empty string")
    if migrated:
        if raw_module_version != "1.0.0":
            raise ProtectionTargetStateError("legacy module_version mismatch")
    elif raw_module_version not in {module_version, *compatible_module_versions}:
        raise ProtectionTargetStateError("module_version mismatch")
    supplied_config_fingerprint = _required_hash(
        state.get("config_fingerprint"), "config_fingerprint"
    )
    expected_config_fingerprint = legacy_config_fingerprint if migrated else config_fingerprint
    if not hmac.compare_digest(supplied_config_fingerprint, expected_config_fingerprint):
        raise ProtectionTargetStateError("config_fingerprint mismatch")
    if not migrated:
        supplied_digest = state.get("state_digest")
        if not isinstance(supplied_digest, str) or not _HEX64.fullmatch(supplied_digest):
            raise ProtectionTargetStateError("state_digest must be lowercase SHA-256")
        unsigned = dict(state)
        unsigned.pop("state_digest", None)
        try:
            expected_digest = _digest(unsigned)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ProtectionTargetStateError("state must be JSON-compatible") from exc
        if not hmac.compare_digest(supplied_digest, expected_digest):
            raise ProtectionTargetStateError("state_digest mismatch")

    bound_symbol = _optional_nonblank(state.get("bound_symbol"), "bound_symbol")
    bound_timeframe = _optional_nonblank(state.get("bound_timeframe"), "bound_timeframe")
    if (bound_symbol is None) != (bound_timeframe is None):
        raise ProtectionTargetStateError("bound_symbol and bound_timeframe must be set together")

    last_bar_time = _optional_datetime(state.get("last_bar_time"), "last_bar_time")
    signature_raw = state.get("last_bar_signature")
    if signature_raw is None:
        signature = None
    else:
        if isinstance(signature_raw, str | bytes) or not isinstance(signature_raw, Sequence):
            raise ProtectionTargetStateError("last_bar_signature must be a sequence")
        if any(not isinstance(item, str) for item in signature_raw):
            raise ProtectionTargetStateError("last_bar_signature items must be strings")
        signature = tuple(signature_raw)
        if len(signature) != 8 or any(not item for item in signature):
            raise ProtectionTargetStateError(
                "last_bar_signature must contain eight non-empty strings"
            )
        if any(len(item) > _MAX_STATE_NUMERIC for item in signature[1:6]):
            raise ProtectionTargetStateError("last_bar_signature numeric item is too large")
        if any(len(item) > _MAX_STATE_IDENTITY for item in signature[6:8]):
            raise ProtectionTargetStateError("last_bar_signature identity is too large")
    if (last_bar_time is None) != (signature is None):
        raise ProtectionTargetStateError(
            "last_bar_time and last_bar_signature must be set together"
        )
    if last_bar_time is not None and bound_symbol is None:
        raise ProtectionTargetStateError("bar cursor requires bound stream identity")
    if signature is not None:
        signature_time = _optional_datetime(signature[0], "last_bar_signature timestamp")
        if signature_time != last_bar_time:
            raise ProtectionTargetStateError(
                "last_bar_signature timestamp must match last_bar_time"
            )
        if signature_time is None:
            raise ProtectionTargetStateError("last_bar_signature timestamp cannot be null")
        signature = (signature_time.isoformat(), *signature[1:])
        if signature[6] != bound_symbol or signature[7] != bound_timeframe:
            raise ProtectionTargetStateError(
                "last_bar_signature stream identity must match bound stream"
            )
        signature_values = [
            _finite_decimal(signature[index], f"last_bar_signature {field_name}")
            for index, field_name in enumerate(("open", "high", "low", "close", "volume"), start=1)
        ]
        open_price, high, low, close, volume = signature_values
        if min(open_price, high, low, close) <= 0:
            raise ProtectionTargetStateError("last_bar_signature prices must be positive")
        if volume < 0:
            raise ProtectionTargetStateError("last_bar_signature volume must be non-negative")
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise ProtectionTargetStateError("last_bar_signature OHLC geometry is inconsistent")

    event_fingerprint_raw = state.get("last_event_fingerprint")
    if migrated or event_fingerprint_raw is None:
        event_fingerprint = None
    else:
        event_fingerprint = _required_hash(event_fingerprint_raw, "last_event_fingerprint")
    if event_fingerprint is not None and last_bar_time is None:
        raise ProtectionTargetStateError("last_event_fingerprint requires a bar cursor")

    processed_raw = state.get("processed", ())
    if isinstance(processed_raw, str | bytes) or not isinstance(processed_raw, Sequence):
        raise ProtectionTargetStateError("processed must be a sequence")
    if len(processed_raw) > max_processed_plans:
        raise ProtectionTargetStateError("processed state exceeds max_processed_plans")
    processed: OrderedDict[str, str] = OrderedDict()
    for row in processed_raw:
        if not isinstance(row, Mapping):
            raise ProtectionTargetStateError("processed rows must be mappings")
        if set(row) != {"process_key", "feature_fingerprint"}:
            raise ProtectionTargetStateError("processed rows have invalid fields")
        process_key = _optional_nonblank(row.get("process_key"), "process_key")
        fingerprint = _required_hash(row.get("feature_fingerprint"), "feature_fingerprint")
        if process_key is None:
            raise ProtectionTargetStateError("processed process_key cannot be null")
        if len(process_key) > 512:
            raise ProtectionTargetStateError("process_key exceeds 512 characters")
        if process_key in processed:
            raise ProtectionTargetStateError("processed process_key values must be unique")
        processed[process_key] = fingerprint

    counters_raw = state.get("counters", {})
    if not isinstance(counters_raw, Mapping):
        raise ProtectionTargetStateError("counters must be a mapping")
    counter_fields = set(Counters().as_mapping())
    if set(counters_raw) != counter_fields:
        raise ProtectionTargetStateError("counters fields are incomplete or unknown")
    parsed_counters: dict[str, int] = {}
    for key in counter_fields:
        value = counters_raw[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_COUNTER
        ):
            raise ProtectionTargetStateError(
                f"counter {key} must be a non-negative integer no greater than {_MAX_COUNTER}"
            )
        parsed_counters[key] = value

    return RestoredState(
        bound_symbol=bound_symbol,
        bound_timeframe=bound_timeframe,
        last_bar_time=last_bar_time,
        last_bar_signature=signature,
        last_event_fingerprint=event_fingerprint,
        processed=processed,
        counters=Counters(**parsed_counters),
        migrated_from_v1=migrated,
    )


def _optional_nonblank(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProtectionTargetStateError(f"{name} must be a non-empty string or null")
    if len(value) > _MAX_STATE_IDENTITY:
        raise ProtectionTargetStateError(f"{name} exceeds {_MAX_STATE_IDENTITY} characters")
    if value != value.strip():
        raise ProtectionTargetStateError(f"{name} cannot contain surrounding whitespace")
    return value


def _required_hash(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ProtectionTargetStateError(f"{name} must be lowercase SHA-256")
    return value


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProtectionTargetStateError(f"{name} must be an ISO-8601 string or null")
    if len(value) > _MAX_STATE_TIMESTAMP:
        raise ProtectionTargetStateError(f"{name} exceeds {_MAX_STATE_TIMESTAMP} characters")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtectionTargetStateError(f"{name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtectionTargetStateError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _finite_decimal(value: object, name: str) -> Decimal:
    text = str(value)
    if len(text) > _MAX_STATE_NUMERIC:
        raise ProtectionTargetStateError(f"{name} numeric value is too large")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProtectionTargetStateError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ProtectionTargetStateError(f"{name} must be finite")
    return result
