from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any


_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FundingDataError(ValueError):
    """Raised when owner-backed funding evidence is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class FundingLineageEntry:
    artifact_id: str
    source_kind: str
    checksum_sha256: str
    record_count: int
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise FundingDataError("funding lineage artifact_id must be non-empty")
        if not isinstance(self.source_kind, str) or not self.source_kind:
            raise FundingDataError("funding lineage source_kind must be non-empty")
        if (
            not isinstance(self.checksum_sha256, str)
            or _SHA256.fullmatch(self.checksum_sha256) is None
        ):
            raise FundingDataError("funding lineage checksum must be lowercase SHA-256")
        if (
            not isinstance(self.record_count, int)
            or isinstance(self.record_count, bool)
            or self.record_count <= 0
        ):
            raise FundingDataError("funding lineage record_count must be positive")
        start = _utc_datetime(self.start, "funding lineage start")
        end = _utc_datetime(self.end, "funding lineage end")
        if end <= start:
            raise FundingDataError("funding lineage interval must be non-empty")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "source_kind": self.source_kind,
            "checksum_sha256": self.checksum_sha256,
            "record_count": self.record_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class FundingEvent:
    ordinal: int
    timestamp: datetime
    symbol: str
    rate: Decimal
    mark_price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.ordinal, int) or isinstance(self.ordinal, bool) or self.ordinal < 0:
            raise FundingDataError("funding event ordinal must be a non-negative integer")
        object.__setattr__(self, "timestamp", _utc_datetime(self.timestamp, "funding timestamp"))
        if not isinstance(self.symbol, str) or _SYMBOL.fullmatch(self.symbol) is None:
            raise FundingDataError("funding symbol must be an uppercase USDT symbol")
        object.__setattr__(self, "rate", _decimal(self.rate, "funding rate"))
        mark_price = _decimal(self.mark_price, "funding mark price")
        if mark_price <= 0:
            raise FundingDataError("funding mark price must be positive")
        object.__setattr__(self, "mark_price", mark_price)

    def key(self, source_identity_sha256: str) -> str:
        if _SHA256.fullmatch(source_identity_sha256) is None:
            raise FundingDataError("funding source identity must be lowercase SHA-256")
        payload = {
            "ordinal": self.ordinal,
            "rate": str(self.rate),
            "mark_price": str(self.mark_price),
            "source_identity_sha256": source_identity_sha256,
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
        }
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class RegisteredFundingSource:
    dataset_id: str
    dataset_version: int
    dataset_hash: str
    symbol: str
    coverage_start_inclusive: datetime
    coverage_end_exclusive: datetime
    event_count: int
    source_record_count: int
    lineage: tuple[FundingLineageEntry, ...]
    events: tuple[FundingEvent, ...]
    schema_version: int = 1
    scope_start_inclusive: datetime | None = None
    scope_end_exclusive: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_id, str) or not self.dataset_id:
            raise FundingDataError("funding dataset_id must be non-empty")
        if (
            not isinstance(self.dataset_version, int)
            or isinstance(self.dataset_version, bool)
            or self.dataset_version <= 0
        ):
            raise FundingDataError("funding dataset_version must be positive")
        if not isinstance(self.dataset_hash, str) or _SHA256.fullmatch(self.dataset_hash) is None:
            raise FundingDataError("funding dataset_hash must be lowercase SHA-256")
        if not isinstance(self.symbol, str) or _SYMBOL.fullmatch(self.symbol) is None:
            raise FundingDataError("funding source symbol must be an uppercase USDT symbol")
        start = _utc_datetime(self.coverage_start_inclusive, "funding coverage start")
        end = _utc_datetime(self.coverage_end_exclusive, "funding coverage end")
        if end <= start:
            raise FundingDataError("funding coverage interval must be non-empty")
        object.__setattr__(self, "coverage_start_inclusive", start)
        object.__setattr__(self, "coverage_end_exclusive", end)
        if self.schema_version not in (1, 2):
            raise FundingDataError("unsupported registered funding source schema")
        if self.schema_version == 1:
            if self.scope_start_inclusive is not None or self.scope_end_exclusive is not None:
                raise FundingDataError("legacy funding source cannot declare execution scope")
            scope_start, scope_end = start, end
        else:
            if self.scope_start_inclusive is None or self.scope_end_exclusive is None:
                raise FundingDataError("scoped funding source requires explicit scope bounds")
            scope_start = _utc_datetime(self.scope_start_inclusive, "funding scope start")
            scope_end = _utc_datetime(self.scope_end_exclusive, "funding scope end")
            if scope_end <= scope_start or scope_start < start or scope_end > end:
                raise FundingDataError("funding scope must be non-empty and within owner coverage")
            object.__setattr__(self, "scope_start_inclusive", scope_start)
            object.__setattr__(self, "scope_end_exclusive", scope_end)
        if (
            not isinstance(self.event_count, int)
            or isinstance(self.event_count, bool)
            or self.event_count <= 0
        ):
            raise FundingDataError("funding event_count must be positive")
        if (
            not isinstance(self.source_record_count, int)
            or isinstance(self.source_record_count, bool)
            or self.source_record_count <= 0
        ):
            raise FundingDataError("funding source_record_count must be positive")

        lineage = tuple(self.lineage)
        events = tuple(self.events)
        if any(not isinstance(item, FundingLineageEntry) for item in lineage):
            raise FundingDataError("funding lineage must contain FundingLineageEntry values")
        if any(not isinstance(event, FundingEvent) for event in events):
            raise FundingDataError("funding events must contain FundingEvent values")
        object.__setattr__(self, "lineage", lineage)
        object.__setattr__(self, "events", events)

        if self.event_count != len(events):
            raise FundingDataError("funding event_count does not match adapted events")
        if self.schema_version == 1 and self.source_record_count != self.event_count:
            raise FundingDataError("funding source_record_count must equal event_count")
        if not lineage:
            raise FundingDataError("funding source requires immutable raw lineage")
        if sum(item.record_count for item in lineage) != self.source_record_count:
            raise FundingDataError("funding lineage counts do not match source_record_count")

        lineage_sorted = tuple(
            sorted(lineage, key=lambda item: (item.start, item.end, item.artifact_id))
        )
        if lineage_sorted != lineage:
            raise FundingDataError("funding lineage must be canonically ordered")
        if lineage[0].start != start:
            raise FundingDataError("funding lineage start differs from owner coverage")
        covered_until = lineage[0].end
        for current in lineage[1:]:
            if current.start > covered_until:
                raise FundingDataError("funding lineage contains an uncovered interval")
            if current.end > covered_until:
                covered_until = current.end
        if covered_until != end:
            raise FundingDataError("funding lineage end differs from owner coverage")

        expected_ordinals = list(range(len(events)))
        if [event.ordinal for event in events] != expected_ordinals:
            raise FundingDataError("funding event ordinals must be contiguous from zero")
        timestamps = [event.timestamp for event in events]
        if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
            raise FundingDataError("funding events must be strictly ordered and unique")
        if any(event.symbol != self.symbol for event in events):
            raise FundingDataError("funding event symbol differs from source identity")
        if any(timestamp < scope_start or timestamp >= scope_end for timestamp in timestamps):
            if self.schema_version == 1:
                raise FundingDataError("funding event falls outside [start,end) owner coverage")
            raise FundingDataError("funding event falls outside scoped [start,end) execution")

    def identity(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_hash": self.dataset_hash,
            "symbol": self.symbol,
            "coverage_start_inclusive": self.coverage_start_inclusive.isoformat(),
            "coverage_end_exclusive": self.coverage_end_exclusive.isoformat(),
            "coverage_semantics": "[start,end)",
            "event_count": self.event_count,
            "source_record_count": self.source_record_count,
            "lineage": [item.to_payload() for item in self.lineage],
        }
        if self.schema_version == 2:
            payload["scope_start_inclusive"] = self.scope_start_inclusive.isoformat()  # type: ignore[union-attr]
            payload["scope_end_exclusive"] = self.scope_end_exclusive.isoformat()  # type: ignore[union-attr]
        return MappingProxyType(payload)

    def identity_sha256(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(dict(self.identity()))).hexdigest()

    def assert_covers(self, start: datetime, end: datetime) -> None:
        requested_start = _utc_datetime(start, "requested funding coverage start")
        requested_end = _utc_datetime(end, "requested funding coverage end")
        if requested_end <= requested_start:
            raise FundingDataError("requested funding coverage interval must be non-empty")
        effective_start = self.scope_start_inclusive or self.coverage_start_inclusive
        effective_end = self.scope_end_exclusive or self.coverage_end_exclusive
        if effective_start > requested_start or effective_end < requested_end:
            raise FundingDataError("registered funding source does not cover requested [start,end)")

    def events_between(self, start: datetime, end: datetime) -> tuple[FundingEvent, ...]:
        self.assert_covers(start, end)
        requested_start = _utc_datetime(start, "requested funding start")
        requested_end = _utc_datetime(end, "requested funding end")
        return tuple(
            event for event in self.events if requested_start <= event.timestamp < requested_end
        )


@dataclass(frozen=True, slots=True)
class FundingSettlement:
    event_key: str
    ordinal: int
    timestamp: datetime
    symbol: str
    side: str
    quantity: Decimal
    notional: Decimal
    rate: Decimal
    cashflow: Decimal

    def to_payload(self) -> dict[str, object]:
        return {
            "event_key": self.event_key,
            "ordinal": self.ordinal,
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "notional": str(self.notional),
            "rate": str(self.rate),
            "cashflow": str(self.cashflow),
        }


class FundingSettlementLedger:
    """Compact exactly-once funding settlement state bound to one immutable source."""

    def __init__(
        self,
        source: RegisteredFundingSource,
        *,
        next_ordinal: int = 0,
        cumulative_cashflow: Decimal = Decimal("0"),
    ) -> None:
        if (
            not isinstance(next_ordinal, int)
            or isinstance(next_ordinal, bool)
            or next_ordinal < 0
            or next_ordinal > source.event_count
        ):
            raise FundingDataError("funding ledger next_ordinal is invalid")
        self._source = source
        self._next_ordinal = next_ordinal
        self._cumulative_cashflow = _decimal(cumulative_cashflow, "funding cumulative cashflow")

    @property
    def next_ordinal(self) -> int:
        return self._next_ordinal

    @property
    def cumulative_cashflow(self) -> Decimal:
        return self._cumulative_cashflow

    def apply(
        self,
        event: FundingEvent,
        *,
        side: str,
        quantity: Decimal,
    ) -> FundingSettlement | None:
        if event.symbol != self._source.symbol:
            raise FundingDataError("funding settlement event symbol differs from source")
        if event.ordinal < self._next_ordinal:
            if (
                event.ordinal >= self._source.event_count
                or self._source.events[event.ordinal] != event
            ):
                raise FundingDataError("replayed funding event differs from immutable source")
            return None
        if event.ordinal != self._next_ordinal:
            raise FundingDataError("funding settlement sequence has a gap")
        if event.ordinal >= self._source.event_count or self._source.events[event.ordinal] != event:
            raise FundingDataError("funding settlement event differs from immutable source")
        normalized_side = side.strip().lower() if isinstance(side, str) else ""
        if normalized_side not in {"long", "short"}:
            raise FundingDataError("funding settlement side must be long or short")
        normalized_quantity = _decimal(quantity, "funding settlement quantity")
        if normalized_quantity <= 0:
            raise FundingDataError("funding settlement quantity must be positive")
        direction = Decimal("1") if normalized_side == "long" else Decimal("-1")
        notional = normalized_quantity * event.mark_price
        cashflow = -(notional * event.rate * direction)
        settlement = FundingSettlement(
            event_key=event.key(self._source.identity_sha256()),
            ordinal=event.ordinal,
            timestamp=event.timestamp,
            symbol=event.symbol,
            side=normalized_side,
            quantity=normalized_quantity,
            notional=notional,
            rate=event.rate,
            cashflow=cashflow,
        )
        self._cumulative_cashflow += cashflow
        self._next_ordinal += 1
        return settlement

    def snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": 1,
                "source_identity_sha256": self._source.identity_sha256(),
                "next_ordinal": self._next_ordinal,
                "cumulative_cashflow": str(self._cumulative_cashflow),
            }
        )

    @classmethod
    def restore(
        cls,
        source: RegisteredFundingSource,
        payload: Mapping[str, object],
    ) -> FundingSettlementLedger:
        expected = {
            "schema_version",
            "source_identity_sha256",
            "next_ordinal",
            "cumulative_cashflow",
        }
        if set(payload) != expected or payload.get("schema_version") != 1:
            raise FundingDataError("funding ledger snapshot schema mismatch")
        if payload.get("source_identity_sha256") != source.identity_sha256():
            raise FundingDataError("funding ledger snapshot belongs to another source")
        next_ordinal = payload.get("next_ordinal")
        cumulative = payload.get("cumulative_cashflow")
        if not isinstance(next_ordinal, int) or isinstance(next_ordinal, bool):
            raise FundingDataError("funding ledger snapshot next_ordinal is invalid")
        if not isinstance(cumulative, str):
            raise FundingDataError("funding ledger snapshot cumulative cashflow is invalid")
        try:
            cumulative_cashflow = Decimal(cumulative)
        except InvalidOperation as exc:
            raise FundingDataError(
                "funding ledger snapshot cumulative cashflow is invalid"
            ) from exc
        return cls(
            source,
            next_ordinal=next_ordinal,
            cumulative_cashflow=cumulative_cashflow,
        )


def open_registered_funding_source(
    storage_root: Path,
    dataset_id: str,
    dataset_version: int,
    *,
    symbol: str,
) -> RegisteredFundingSource:
    """Adapt the accepted Market Data funding selection without moving data ownership."""

    try:
        from market_data_ingestion.errors import StructuredIngestionError
        from market_data_ingestion.funding_dataset_reader import (
            iter_registered_funding,
            resolve_registered_funding_dataset,
        )
    except ImportError as exc:
        raise FundingDataError(
            "registered funding support requires modular-market-data-ingestion with parquet support"
        ) from exc

    try:
        selection = resolve_registered_funding_dataset(
            storage_root,
            dataset_id,
            dataset_version,
            consumer="modular_backtest",
            deep=True,
        )
        return adapt_registered_funding_selection(
            selection,
            iter_registered_funding(selection),
            expected_symbol=symbol,
        )
    except FundingDataError:
        raise
    except (StructuredIngestionError, OSError, TypeError, ValueError) as exc:
        raise FundingDataError(f"registered funding validation failed: {exc}") from exc


def adapt_registered_funding_selection(
    selection: Any,
    records: Iterable[Any],
    *,
    expected_symbol: str,
) -> RegisteredFundingSource:
    """Pure adapter used by the registered read path and focused contract tests."""

    manifest_symbol = str(selection.manifest.instrument.symbol)
    if manifest_symbol != expected_symbol:
        raise FundingDataError("registered funding symbol differs from backtest configuration")

    lineage = tuple(
        sorted(
            (
                FundingLineageEntry(
                    artifact_id=str(item.artifact_id),
                    source_kind=str(item.source.value),
                    checksum_sha256=str(item.checksum_sha256),
                    record_count=_required_record_count(item.record_count),
                    start=item.start,
                    end=item.end,
                )
                for item in selection.source_artifacts
            ),
            key=lambda item: (item.start, item.end, item.artifact_id),
        )
    )
    events: list[FundingEvent] = []
    for ordinal, record in enumerate(records):
        if record.mark_price is None:
            raise FundingDataError("registered funding event is missing owner mark_price evidence")
        if str(record.symbol) != expected_symbol:
            raise FundingDataError(
                "registered funding event symbol differs from backtest configuration"
            )
        events.append(
            FundingEvent(
                ordinal=ordinal,
                timestamp=record.funding_time,
                symbol=str(record.symbol),
                rate=record.funding_rate,
                mark_price=record.mark_price,
            )
        )

    descriptor = selection.descriptor
    source = RegisteredFundingSource(
        dataset_id=str(descriptor.dataset_id),
        dataset_version=int(descriptor.version),
        dataset_hash=str(descriptor.dataset_hash),
        symbol=expected_symbol,
        coverage_start_inclusive=selection.coverage_start_inclusive,
        coverage_end_exclusive=selection.coverage_end_exclusive,
        event_count=int(selection.event_count),
        source_record_count=int(selection.source_record_count),
        lineage=lineage,
        events=tuple(events),
    )
    if source.event_count != len(events):
        raise FundingDataError("registered funding adapter did not consume every owner event")
    return source


def _required_record_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FundingDataError("registered funding lineage record_count is missing or invalid")
    return value


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FundingDataError(f"{name} must be timezone-aware UTC")
    offset = value.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise FundingDataError(f"{name} must use UTC offset")
    return value.astimezone(UTC)


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise FundingDataError(f"{name} must be Decimal")
    if not value.is_finite():
        raise FundingDataError(f"{name} must be finite")
    return value


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FundingDataError("funding identity is not canonical JSON") from exc
