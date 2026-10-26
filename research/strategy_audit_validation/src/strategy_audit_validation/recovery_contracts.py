"""Fail-closed validation for recovered canonical-strategy control evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Final

from .errors import EvidenceMalformedError
from .governance import CANONICAL_COMPOSITION_NAME

CONTROL_EVIDENCE_SCHEMA_VERSION: Final = 1
EXPECTED_COMPONENT_TRADES: Final = MappingProxyType(
    {
        "fvg_rt_final_15m": 283,
        "frozen_1h_short": 91,
        "frozen_4h_short": 21,
    }
)
EXPECTED_COMBINED_TRADES: Final = 395
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class ControlDatasetIdentity:
    dataset_id: str
    version: int
    dataset_hash: str
    exchange: str
    market: str
    symbol: str
    base_timeframe: str
    timezone: str
    start: datetime
    end: datetime
    row_count: int
    gap_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class LedgerReference:
    module_id: str
    expected_trade_count: int
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CombinedLedgerReference:
    expected_trade_count: int
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ControlEvidenceManifest:
    composition_name: str
    dataset: ControlDatasetIdentity
    execution_contract_sha256: str
    component_ledgers: tuple[LedgerReference, ...]
    combined_ledger: CombinedLedgerReference
    signal_counts_before_portfolio: Mapping[str, int]
    semantic_digest: str
    generated_from_commit: str


def load_control_evidence_manifest(payload: Mapping[str, Any]) -> ControlEvidenceManifest:
    """Validate one exact control-evidence manifest without reading ledger bytes."""

    _exact_keys(
        payload,
        {
            "schema_version",
            "composition_name",
            "dataset",
            "execution_contract_sha256",
            "component_ledgers",
            "combined_ledger",
            "signal_counts_before_portfolio",
            "semantic_digest",
            "generated_from_commit",
        },
        "control evidence manifest",
    )
    if payload["schema_version"] != CONTROL_EVIDENCE_SCHEMA_VERSION:
        raise EvidenceMalformedError("unsupported control evidence schema_version")
    if payload["composition_name"] != CANONICAL_COMPOSITION_NAME:
        raise EvidenceMalformedError("control evidence composition_name mismatch")

    dataset = _load_dataset(_mapping(payload["dataset"], "dataset"))
    execution_contract_sha256 = _sha256(
        payload["execution_contract_sha256"], "execution_contract_sha256"
    )
    component_ledgers = _load_component_ledgers(
        _sequence(payload["component_ledgers"], "component_ledgers")
    )
    combined_ledger = _load_combined_ledger(_mapping(payload["combined_ledger"], "combined_ledger"))
    signal_counts = _load_signal_counts(
        _mapping(payload["signal_counts_before_portfolio"], "signal_counts_before_portfolio")
    )
    semantic_digest = _sha256(payload["semantic_digest"], "semantic_digest")
    generated_from_commit = _git_sha(payload["generated_from_commit"], "generated_from_commit")

    return ControlEvidenceManifest(
        composition_name=CANONICAL_COMPOSITION_NAME,
        dataset=dataset,
        execution_contract_sha256=execution_contract_sha256,
        component_ledgers=component_ledgers,
        combined_ledger=combined_ledger,
        signal_counts_before_portfolio=MappingProxyType(signal_counts),
        semantic_digest=semantic_digest,
        generated_from_commit=generated_from_commit,
    )


def _load_dataset(payload: Mapping[str, Any]) -> ControlDatasetIdentity:
    _exact_keys(
        payload,
        {
            "dataset_id",
            "version",
            "dataset_hash",
            "exchange",
            "market",
            "symbol",
            "base_timeframe",
            "timezone",
            "start",
            "end",
            "row_count",
            "gap_count",
            "duplicate_count",
        },
        "dataset",
    )
    dataset_id = _non_blank(payload["dataset_id"], "dataset_id")
    version = _positive_int(payload["version"], "dataset version")
    dataset_hash = _sha256(payload["dataset_hash"], "dataset_hash")
    if payload["exchange"] != "binance":
        raise EvidenceMalformedError("control dataset exchange must be binance")
    if payload["market"] != "usdt_m_perpetual":
        raise EvidenceMalformedError("control dataset market must be usdt_m_perpetual")
    if payload["symbol"] != "BTCUSDT":
        raise EvidenceMalformedError("control dataset symbol must be BTCUSDT")
    if payload["base_timeframe"] != "5m":
        raise EvidenceMalformedError("control dataset base_timeframe must be 5m")
    if payload["timezone"] != "UTC":
        raise EvidenceMalformedError("control dataset timezone must be UTC")
    start = _timestamp(payload["start"], "dataset start")
    end = _timestamp(payload["end"], "dataset end")
    if start >= end:
        raise EvidenceMalformedError("control dataset start must be before end")
    row_count = _positive_int(payload["row_count"], "row_count")
    gap_count = _non_negative_int(payload["gap_count"], "gap_count")
    duplicate_count = _non_negative_int(payload["duplicate_count"], "duplicate_count")
    return ControlDatasetIdentity(
        dataset_id=dataset_id,
        version=version,
        dataset_hash=dataset_hash,
        exchange="binance",
        market="usdt_m_perpetual",
        symbol="BTCUSDT",
        base_timeframe="5m",
        timezone="UTC",
        start=start,
        end=end,
        row_count=row_count,
        gap_count=gap_count,
        duplicate_count=duplicate_count,
    )


def _load_component_ledgers(values: tuple[Any, ...]) -> tuple[LedgerReference, ...]:
    ledgers: dict[str, LedgerReference] = {}
    for value in values:
        raw = _mapping(value, "component ledger")
        _exact_keys(
            raw,
            {"module_id", "expected_trade_count", "path", "sha256"},
            "component ledger",
        )
        module_id = _non_blank(raw["module_id"], "component ledger module_id")
        if module_id not in EXPECTED_COMPONENT_TRADES:
            raise EvidenceMalformedError(f"unknown component ledger module_id: {module_id}")
        if module_id in ledgers:
            raise EvidenceMalformedError(f"duplicate component ledger: {module_id}")
        expected = _positive_int(raw["expected_trade_count"], "expected_trade_count")
        if expected != EXPECTED_COMPONENT_TRADES[module_id]:
            raise EvidenceMalformedError(f"control trade count mismatch for {module_id}")
        ledgers[module_id] = LedgerReference(
            module_id=module_id,
            expected_trade_count=expected,
            path=_relative_path(raw["path"], f"{module_id} ledger path"),
            sha256=_sha256(raw["sha256"], f"{module_id} ledger sha256"),
        )
    if set(ledgers) != set(EXPECTED_COMPONENT_TRADES):
        missing = sorted(set(EXPECTED_COMPONENT_TRADES) - set(ledgers))
        raise EvidenceMalformedError(f"component control ledgers incomplete: {missing}")
    return tuple(ledgers[module_id] for module_id in sorted(ledgers))


def _load_combined_ledger(payload: Mapping[str, Any]) -> CombinedLedgerReference:
    _exact_keys(payload, {"expected_trade_count", "path", "sha256"}, "combined ledger")
    count = _positive_int(payload["expected_trade_count"], "combined expected_trade_count")
    if count != EXPECTED_COMBINED_TRADES:
        raise EvidenceMalformedError("combined control trade count must be 395")
    return CombinedLedgerReference(
        expected_trade_count=count,
        path=_relative_path(payload["path"], "combined ledger path"),
        sha256=_sha256(payload["sha256"], "combined ledger sha256"),
    )


def _load_signal_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    if set(payload) != set(EXPECTED_COMPONENT_TRADES):
        raise EvidenceMalformedError("signal_counts_before_portfolio must cover every component")
    return {
        module_id: _non_negative_int(payload[module_id], f"{module_id} signal count")
        for module_id in sorted(EXPECTED_COMPONENT_TRADES)
    }


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise EvidenceMalformedError(f"{name} must be a string-keyed mapping")
    return value


def _sequence(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvidenceMalformedError(f"{name} must be an array")
    return tuple(value)


def _exact_keys(payload: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = expected - set(payload)
    unknown = set(payload) - expected
    if missing or unknown:
        raise EvidenceMalformedError(
            f"{name} fields differ from schema; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _non_blank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceMalformedError(f"{name} must be a non-blank trimmed string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceMalformedError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvidenceMalformedError(f"{name} must be a non-negative integer")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase SHA-256")
    return value


def _git_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise EvidenceMalformedError(f"{name} must be a lowercase 40-character Git SHA")
    return value


def _relative_path(value: object, name: str) -> str:
    rendered = _non_blank(value, name)
    path = PurePosixPath(rendered)
    if path.is_absolute() or ".." in path.parts or rendered.startswith("./"):
        raise EvidenceMalformedError(f"{name} must be a normalized relative path")
    if path.as_posix() != rendered:
        raise EvidenceMalformedError(f"{name} must use normalized POSIX separators")
    return rendered


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceMalformedError(f"{name} must be ISO-8601")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise EvidenceMalformedError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceMalformedError(f"{name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise EvidenceMalformedError(f"{name} must use UTC offset")
    return parsed.astimezone(UTC)


__all__ = [
    "CONTROL_EVIDENCE_SCHEMA_VERSION",
    "EXPECTED_COMBINED_TRADES",
    "EXPECTED_COMPONENT_TRADES",
    "CombinedLedgerReference",
    "ControlDatasetIdentity",
    "ControlEvidenceManifest",
    "LedgerReference",
    "load_control_evidence_manifest",
]
