from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from modular_core import (
    EventEnvelope,
    EventStore,
    OrderSnapshot,
    PositionSnapshot,
    StateRepository,
)

from .canonical import canonical_sha256, require_json_mapping, to_jsonable
from .cycle import PaperBarRecoveryDecision
from .market_data import MarketEventEnvelope
from .routing import FactualEventEnvelope

_SOURCE_STATE_SCHEMA = "1"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _sequence(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _canonical_datetime(value: object, name: str) -> Mapping[str, str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} is not valid ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be datetime or ISO-8601 text")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return {"$datetime": parsed.astimezone(UTC).isoformat()}


class PendingOrderSnapshotBoundary(Protocol):
    def pending_snapshots(self) -> tuple[OrderSnapshot, ...]: ...


class PositionSnapshotBoundary(Protocol):
    def position_snapshot(self, mark_price: float) -> PositionSnapshot | None: ...


class CoreVenueEvidence:
    """Canonical, read-only evidence over Core venue and journal state."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        pending_orders: PendingOrderSnapshotBoundary,
        positions: PositionSnapshotBoundary,
    ) -> None:
        if not callable(getattr(pending_orders, "pending_snapshots", None)):
            raise TypeError("pending_orders must implement pending_snapshots()")
        if not callable(getattr(positions, "position_snapshot", None)):
            raise TypeError("positions must implement position_snapshot()")
        self.event_store = event_store
        self.pending_orders = pending_orders
        self.positions = positions

    def snapshot_evidence(self, event: MarketEventEnvelope) -> Mapping[str, Any]:
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        records = self.event_store.read()
        sequence = records[-1].sequence if records else 0
        pending = self.pending_orders.pending_snapshots()
        if any(not isinstance(item, OrderSnapshot) for item in pending):
            raise TypeError("pending_snapshots() returned a non-OrderSnapshot")
        ordered = tuple(sorted(pending, key=lambda item: item.id))
        position = self.positions.position_snapshot(float(event.candle.close))
        if position is not None and not isinstance(position, PositionSnapshot):
            raise TypeError("position_snapshot() returned an invalid value")
        return require_json_mapping(
            {
                "core_event_sequence": sequence,
                "pending_orders": to_jsonable(ordered),
                "position": to_jsonable(position),
                "symbol": event.candle.symbol,
                "mark_price": event.candle.close,
            },
            "Core venue evidence",
        )


class ConservativePaperBarReconciliation:
    """Retry unchanged state and block every changed ambiguous crash window."""

    def resolve(
        self,
        *,
        event: MarketEventEnvelope,
        before: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> PaperBarRecoveryDecision:
        del event
        if dict(before) == dict(current):
            return PaperBarRecoveryDecision.RETRY
        return PaperBarRecoveryDecision.BLOCKED


class CoreJournalFactualSource:
    """Durable cursor and decoder over canonical Core order/trade events."""

    def __init__(
        self,
        *,
        run_id: str,
        core_run_id: str,
        portfolio_id: str,
        instruments: Sequence[str],
        config_hash: str,
        event_store: EventStore,
        state_repository: StateRepository,
    ) -> None:
        self.run_id = _text(run_id, "run_id")
        self.core_run_id = _text(core_run_id, "core_run_id")
        self.portfolio_id = _text(portfolio_id, "portfolio_id")
        if isinstance(instruments, (str, bytes)) or not isinstance(instruments, Sequence):
            raise TypeError("instruments must be a sequence")
        normalized = tuple(_text(item, "instrument") for item in instruments)
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("instruments must be non-empty and unique")
        self.instruments = normalized
        self.config_hash = _digest(config_hash, "config_hash")
        self.event_store = event_store
        self.state_repository = state_repository
        self._cursor = 0
        self._offered: tuple[int, tuple[FactualEventEnvelope, ...]] | None = None
        self._load_state()

    @property
    def state_key(self) -> str:
        return f"trading-runner-core-factual:{self.run_id}"

    @property
    def cursor(self) -> int:
        return self._cursor

    def events_for(self, event: MarketEventEnvelope) -> Sequence[FactualEventEnvelope]:
        if not isinstance(event, MarketEventEnvelope):
            raise TypeError("event must be MarketEventEnvelope")
        if self._offered is not None:
            return self._offered[1]
        records = self.event_store.read(after_sequence=self._cursor)
        through = records[-1].sequence if records else self._cursor
        factual: list[FactualEventEnvelope] = []
        for record in records:
            if record.run_id != self.core_run_id:
                raise ValueError("Core factual event stream run_id mismatch")
            decoded = self._decode(record, fallback_symbol=event.candle.symbol)
            if decoded is not None:
                factual.append(decoded)
        offered = tuple(factual)
        self._offered = (through, offered)
        return offered

    def commit(self, events: Sequence[FactualEventEnvelope]) -> None:
        if self._offered is None:
            raise RuntimeError("no factual batch is awaiting commit")
        if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
            raise TypeError("events must be a sequence")
        normalized = tuple(events)
        if any(not isinstance(item, FactualEventEnvelope) for item in normalized):
            raise TypeError("events contains a non-factual envelope")
        through, expected_events = self._offered
        expected = tuple((item.event_id, item.content_hash) for item in expected_events)
        observed = tuple((item.event_id, item.content_hash) for item in normalized)
        if observed != expected:
            raise ValueError("factual batch identity changed before commit")
        payload: dict[str, Any] = {
            "schema_version": _SOURCE_STATE_SCHEMA,
            "run_id": self.run_id,
            "core_run_id": self.core_run_id,
            "portfolio_id": self.portfolio_id,
            "instruments": self.instruments,
            "config_hash": self.config_hash,
            "cursor": through,
        }
        payload["integrity_hash"] = canonical_sha256(payload)
        self.state_repository.save(self.state_key, payload)
        self._cursor = through
        self._offered = None

    def _load_state(self) -> None:
        state = self.state_repository.load(self.state_key)
        if state is None:
            return
        payload = dict(state)
        stored = _digest(payload.pop("integrity_hash", None), "integrity_hash")
        if canonical_sha256(payload) != stored:
            raise ValueError("Core factual cursor integrity check failed")
        raw_instruments = payload.get("instruments")
        if isinstance(raw_instruments, (str, bytes)) or not isinstance(raw_instruments, Sequence):
            raise ValueError("Core factual cursor instruments are invalid")
        stored_instruments = tuple(raw_instruments)
        if (
            payload.get("schema_version") != _SOURCE_STATE_SCHEMA
            or payload.get("run_id") != self.run_id
            or payload.get("core_run_id") != self.core_run_id
            or payload.get("portfolio_id") != self.portfolio_id
            or stored_instruments != self.instruments
            or payload.get("config_hash") != self.config_hash
        ):
            raise ValueError("Core factual cursor identity is incompatible")
        cursor = _sequence(payload.get("cursor"), "cursor")
        records = self.event_store.read()
        latest = records[-1].sequence if records else 0
        if cursor > latest:
            raise ValueError("Core factual cursor is ahead of the event store")
        self._cursor = cursor

    def _decode(
        self,
        record: EventEnvelope,
        *,
        fallback_symbol: str,
    ) -> FactualEventEnvelope | None:
        if record.event_type.startswith("order."):
            kind = "order"
            raw_payload = record.payload.get("order")
            if not isinstance(raw_payload, Mapping):
                raise ValueError("Core order event is missing OrderSnapshot payload")
            payload = dict(raw_payload)
            payload["created_at"] = _canonical_datetime(
                payload.get("created_at"), "order.created_at"
            )
            for field in ("good_till_time", "triggered_at"):
                if payload.get(field) is not None:
                    payload[field] = _canonical_datetime(payload[field], f"order.{field}")
            decoded_payload: Mapping[str, Any] = {
                "timestamp": _canonical_datetime(record.timestamp, "order timestamp"),
                "event": record.event_type.removeprefix("order."),
                "order": payload,
                "reason": record.payload.get("reason"),
                "event_id": record.event_id,
                "correlation_id": record.correlation_id,
                "causation_id": record.causation_id,
            }
        elif record.event_type == "position.closed":
            kind = "trade"
            raw_payload = record.payload.get("trade")
            if not isinstance(raw_payload, Mapping):
                raise ValueError("Core trade event is missing Trade payload")
            payload = dict(raw_payload)
            for field in ("entry_time", "exit_time"):
                payload[field] = _canonical_datetime(payload.get(field), f"trade.{field}")
            decoded_payload = payload
        else:
            return None
        symbol = _text(record.symbol or fallback_symbol, "factual symbol")
        if symbol not in self.instruments:
            raise ValueError(f"Core factual event symbol is not configured: {symbol}")
        mapping: dict[str, Any] = {
            "schema_version": "1",
            "event_id": f"core:{record.event_id}",
            "aggregate_id": f"{self.portfolio_id}:{symbol}",
            "symbol": symbol,
            "timestamp": to_jsonable(record.timestamp),
            "kind": kind,
            "payload": decoded_payload,
            "correlation_id": record.correlation_id,
            "causation_id": record.causation_id,
        }
        return FactualEventEnvelope.from_mapping(mapping)
