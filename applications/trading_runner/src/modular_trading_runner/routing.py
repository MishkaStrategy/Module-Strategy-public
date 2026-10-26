from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from modular_core import (
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    StateRepository,
    TimeInForce,
    Trade,
)

from .canonical import canonical_sha256, require_json_mapping, to_jsonable
from .errors import (
    FactualEventIdentityConflict,
    FactualEventRecoveryRequired,
    FactualEventRoutingError,
)
from .journal import RuntimeEventJournal
from .market_data import BoundedCriticalQueue
from .ports import RuntimeClock

_STATE_SCHEMA = "1"
_MAX_PROCESSED_EVENTS = 1_000_000


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 256:
        raise ValueError(f"{name} exceeds maximum length")
    return value


def _required_text(value: object, name: str) -> str:
    text = _text(value, name)
    assert text is not None
    return text


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _datetime_from_json(value: object, name: str) -> datetime:
    if not isinstance(value, Mapping) or set(value) != {"$datetime"}:
        raise ValueError(f"{name} must be a canonical datetime mapping")
    raw = value["$datetime"]
    if not isinstance(raw, str):
        raise ValueError(f"{name} canonical datetime must be a string")
    try:
        return _utc(datetime.fromisoformat(raw), name)
    except ValueError as exc:
        raise ValueError(f"{name} is not valid ISO-8601") from exc


def _required_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result < float("inf") or not result > float("-inf"):
        raise ValueError(f"{name} must be finite")
    return result


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be int")
    return value


def _required_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _enum[T](enum_type: type[T], value: object, name: str) -> T:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string enum value")
    factory = cast(Callable[[object], T], enum_type)
    try:
        return factory(value)
    except ValueError as exc:
        raise ValueError(f"unsupported {name}: {value!r}") from exc


class FactualEventKind(StrEnum):
    ORDER = "order"
    TRADE = "trade"


@dataclass(frozen=True, slots=True)
class FactualEventEnvelope:
    event_id: str
    aggregate_id: str
    symbol: str
    timestamp: datetime
    kind: FactualEventKind
    payload: OrderEvent | Trade
    correlation_id: str | None = None
    causation_id: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("event_id", "aggregate_id", "symbol", "schema_version"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        for name in ("correlation_id", "causation_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, optional=True))
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        if self.schema_version != "1":
            raise ValueError("unsupported factual event schema_version")
        if not isinstance(self.kind, FactualEventKind):
            raise TypeError("kind must be FactualEventKind")
        if self.kind is FactualEventKind.ORDER:
            if not isinstance(self.payload, OrderEvent):
                raise TypeError("order factual event requires Core OrderEvent payload")
            if _utc(self.payload.timestamp, "order event timestamp") != self.timestamp:
                raise ValueError("envelope timestamp must equal OrderEvent.timestamp")
        elif self.kind is FactualEventKind.TRADE:
            if not isinstance(self.payload, Trade):
                raise TypeError("trade factual event requires Core Trade payload")
            if _utc(self.payload.exit_time, "trade exit_time") != self.timestamp:
                raise ValueError("envelope timestamp must equal Trade.exit_time")

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self.to_mapping(include_hash=False))

    def to_mapping(self, *, include_hash: bool = True) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "aggregate_id": self.aggregate_id,
            "symbol": self.symbol,
            "timestamp": to_jsonable(self.timestamp),
            "kind": self.kind.value,
            "payload": to_jsonable(self.payload),
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }
        if include_hash:
            result["content_hash"] = self.content_hash
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FactualEventEnvelope:
        if not isinstance(value, Mapping):
            raise TypeError("factual event mapping must be a mapping")
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("factual event payload mapping is required")
        kind = _enum(FactualEventKind, value.get("kind"), "factual event kind")
        decoded: OrderEvent | Trade
        if kind is FactualEventKind.ORDER:
            decoded = _decode_order_event(payload)
        else:
            decoded = _decode_trade(payload)
        envelope = cls(
            event_id=_required_text(value.get("event_id"), "event_id"),
            aggregate_id=_required_text(value.get("aggregate_id"), "aggregate_id"),
            symbol=_required_text(value.get("symbol"), "symbol"),
            timestamp=_datetime_from_json(value.get("timestamp"), "timestamp"),
            kind=kind,
            payload=decoded,
            correlation_id=_optional_text(value.get("correlation_id")),
            causation_id=_optional_text(value.get("causation_id")),
            schema_version=_required_text(value.get("schema_version"), "schema_version"),
        )
        stored_hash = value.get("content_hash")
        if (
            stored_hash is not None
            and _digest(stored_hash, "content_hash") != envelope.content_hash
        ):
            raise ValueError("factual event content_hash does not match payload")
        return envelope


class FactualEventConsumer(Protocol):
    def on_order_event(self, event: OrderEvent) -> None: ...

    def on_trade(self, trade: Trade) -> None: ...

    def snapshot_state(self) -> Mapping[str, Any]: ...

    def restore_state(self, state: Mapping[str, Any]) -> None: ...


class PortfolioEvidenceProvider(Protocol):
    def snapshot_evidence(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FactualRouteResult:
    event_id: str
    content_hash: str
    duplicate: bool
    portfolio_evidence: Mapping[str, Any]


class FactualEventRouter:
    """Crash-consistent factual event routing into Position & Exit only."""

    def __init__(
        self,
        *,
        run_id: str,
        config_hash: str,
        module_graph_hash: str,
        consumer: FactualEventConsumer,
        state_repository: StateRepository,
        journal: RuntimeEventJournal,
        clock: RuntimeClock,
        queue_capacity: int,
        portfolio_evidence: PortfolioEvidenceProvider | None = None,
    ) -> None:
        self.run_id = _required_text(run_id, "run_id")
        self.config_hash = _digest(config_hash, "config_hash")
        self.module_graph_hash = _digest(module_graph_hash, "module_graph_hash")
        if not callable(getattr(consumer, "on_order_event", None)) or not callable(
            getattr(consumer, "on_trade", None)
        ):
            raise TypeError("consumer must implement factual event handlers")
        if not callable(getattr(consumer, "snapshot_state", None)) or not callable(
            getattr(consumer, "restore_state", None)
        ):
            raise TypeError("consumer must implement snapshot/restore")
        self.consumer = consumer
        self.state_repository = state_repository
        self.journal = journal
        self.clock = clock
        self.portfolio_evidence = portfolio_evidence
        self.queue: BoundedCriticalQueue[FactualEventEnvelope] = BoundedCriticalQueue(
            queue_capacity
        )
        self._processed: dict[str, str] = {}
        self._pending_processing: Mapping[str, Any] | None = None
        self._pending_terminal: Mapping[str, Any] | None = None
        self._load_state()

    @property
    def recovery_required(self) -> bool:
        return self._pending_processing is not None or self._pending_terminal is not None

    def enqueue(self, event: FactualEventEnvelope) -> None:
        if not isinstance(event, FactualEventEnvelope):
            raise TypeError("event must be FactualEventEnvelope")
        self.queue.put(event)

    def process_next(self) -> FactualRouteResult:
        return self.route(self.queue.get())

    def route(self, event: FactualEventEnvelope) -> FactualRouteResult:
        if not isinstance(event, FactualEventEnvelope):
            raise TypeError("event must be FactualEventEnvelope")
        existing_hash = self._processed.get(event.event_id)
        if existing_hash is not None:
            if existing_hash != event.content_hash:
                raise FactualEventIdentityConflict(
                    "factual event_id was reused with different content",
                    reason_code="factual_event_identity_conflict",
                )
            return FactualRouteResult(
                event_id=event.event_id,
                content_hash=event.content_hash,
                duplicate=True,
                portfolio_evidence={},
            )
        if self.recovery_required:
            raise FactualEventRecoveryRequired(
                "unfinished factual event must be recovered before new routing",
                reason_code="factual_event_recovery_required",
            )
        if len(self._processed) >= _MAX_PROCESSED_EVENTS:
            raise FactualEventRoutingError(
                "processed factual identity limit exceeded",
                reason_code="factual_event_identity_limit",
            )

        pre_state = require_json_mapping(self.consumer.snapshot_state(), "consumer snapshot")
        self._pending_processing = {
            "event": event.to_mapping(),
            "consumer_state": pre_state,
        }
        self._save_state()
        try:
            self.journal.append(
                timestamp=self.clock.now(),
                event_type="factual_event.processing_started",
                aggregate_id=event.aggregate_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                symbol=event.symbol,
                payload={
                    "event_id": event.event_id,
                    "kind": event.kind.value,
                    "content_hash": event.content_hash,
                },
            )
            self._apply(event)
            evidence = self._portfolio_evidence()
            self._pending_terminal = {
                "event": event.to_mapping(),
                "portfolio_evidence": evidence,
            }
            self._pending_processing = None
            self._save_state()
            self._append_terminal(event, evidence)
            self._processed[event.event_id] = event.content_hash
            self._pending_terminal = None
            self._save_state()
            return FactualRouteResult(
                event_id=event.event_id,
                content_hash=event.content_hash,
                duplicate=False,
                portfolio_evidence=evidence,
            )
        except Exception:
            if self._pending_processing is not None:
                try:
                    self.consumer.restore_state(pre_state)
                except Exception as restore_error:
                    raise FactualEventRoutingError(
                        "factual event failed and consumer rollback failed",
                        reason_code="factual_event_rollback_failed",
                    ) from restore_error
            raise

    def recover(self) -> FactualRouteResult | None:
        if self._pending_terminal is not None:
            event_raw = self._pending_terminal.get("event")
            if not isinstance(event_raw, Mapping):
                raise FactualEventRecoveryRequired(
                    "pending terminal event is invalid",
                    reason_code="factual_routing_state_invalid",
                )
            event = FactualEventEnvelope.from_mapping(event_raw)
            evidence = require_json_mapping(
                self._pending_terminal.get("portfolio_evidence", {}),
                "portfolio evidence",
            )
            if not self._terminal_exists(event):
                self._append_terminal(event, evidence)
            self._processed[event.event_id] = event.content_hash
            self._pending_terminal = None
            self._save_state()
            return FactualRouteResult(
                event_id=event.event_id,
                content_hash=event.content_hash,
                duplicate=False,
                portfolio_evidence=evidence,
            )
        if self._pending_processing is None:
            return None
        event_raw = self._pending_processing.get("event")
        consumer_raw = self._pending_processing.get("consumer_state")
        if not isinstance(event_raw, Mapping):
            raise FactualEventRecoveryRequired(
                "pending processing event is invalid",
                reason_code="factual_routing_state_invalid",
            )
        event = FactualEventEnvelope.from_mapping(event_raw)
        pre_state = require_json_mapping(consumer_raw, "consumer snapshot")
        self.consumer.restore_state(pre_state)
        self._pending_processing = None
        self._save_state()
        return self.route(event)

    def _apply(self, event: FactualEventEnvelope) -> None:
        if event.kind is FactualEventKind.ORDER:
            assert isinstance(event.payload, OrderEvent)
            self.consumer.on_order_event(event.payload)
        else:
            assert isinstance(event.payload, Trade)
            self.consumer.on_trade(event.payload)

    def _portfolio_evidence(self) -> Mapping[str, Any]:
        if self.portfolio_evidence is None:
            return {}
        return require_json_mapping(
            self.portfolio_evidence.snapshot_evidence(),
            "portfolio evidence",
        )

    def _append_terminal(
        self,
        event: FactualEventEnvelope,
        evidence: Mapping[str, Any],
    ) -> None:
        self.journal.append(
            timestamp=self.clock.now(),
            event_type="factual_event.processed",
            aggregate_id=event.aggregate_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            symbol=event.symbol,
            payload={
                "event_id": event.event_id,
                "kind": event.kind.value,
                "content_hash": event.content_hash,
                "portfolio_evidence": evidence,
            },
        )

    def _terminal_exists(self, event: FactualEventEnvelope) -> bool:
        for record in self.journal.read("factual_event.processed"):
            if (
                record.payload.get("event_id") == event.event_id
                and record.payload.get("content_hash") == event.content_hash
            ):
                return True
        return False

    @property
    def _state_key(self) -> str:
        return f"trading-runner-routing:{self.run_id}"

    def _state_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": _STATE_SCHEMA,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "module_graph_hash": self.module_graph_hash,
            "processed": dict(sorted(self._processed.items())),
            "pending_processing": self._pending_processing,
            "pending_terminal": self._pending_terminal,
        }
        payload["integrity_hash"] = canonical_sha256(payload)
        return payload

    def _save_state(self) -> None:
        self.state_repository.save(self._state_key, self._state_payload())

    def _load_state(self) -> None:
        snapshot = self.state_repository.load(self._state_key)
        if snapshot is None:
            return
        payload = dict(snapshot)
        stored_integrity = _digest(payload.pop("integrity_hash", None), "integrity_hash")
        if canonical_sha256(payload) != stored_integrity:
            raise FactualEventRecoveryRequired(
                "factual routing state integrity check failed",
                reason_code="factual_routing_state_corrupt",
            )
        if (
            payload.get("schema_version") != _STATE_SCHEMA
            or payload.get("run_id") != self.run_id
            or payload.get("config_hash") != self.config_hash
            or payload.get("module_graph_hash") != self.module_graph_hash
        ):
            raise FactualEventRecoveryRequired(
                "factual routing state identity is incompatible",
                reason_code="factual_routing_state_incompatible",
            )
        processed = payload.get("processed", {})
        if not isinstance(processed, Mapping):
            raise FactualEventRecoveryRequired(
                "processed factual identities must be a mapping",
                reason_code="factual_routing_state_invalid",
            )
        if len(processed) > _MAX_PROCESSED_EVENTS:
            raise FactualEventRecoveryRequired(
                "processed factual identity limit exceeded",
                reason_code="factual_routing_state_invalid",
            )
        self._processed = {
            _required_text(event_id, "processed event_id"): _digest(
                content_hash,
                "processed content_hash",
            )
            for event_id, content_hash in processed.items()
        }
        for name in ("pending_processing", "pending_terminal"):
            value = payload.get(name)
            if value is not None and not isinstance(value, Mapping):
                raise FactualEventRecoveryRequired(
                    f"{name} must be a mapping or null",
                    reason_code="factual_routing_state_invalid",
                )
            setattr(self, f"_{name}", value)


def _decode_order_event(value: Mapping[str, Any]) -> OrderEvent:
    order_raw = value.get("order")
    if not isinstance(order_raw, Mapping):
        raise ValueError("OrderEvent.order mapping is required")
    order = OrderSnapshot(
        id=_required_text(order_raw.get("id"), "order.id"),
        source_module=_required_text(order_raw.get("source_module"), "order.source_module"),
        created_at=_datetime_from_json(order_raw.get("created_at"), "order.created_at"),
        side=_enum(Side, order_raw.get("side"), "order.side"),
        order_type=_enum(OrderType, order_raw.get("order_type"), "order.order_type"),
        quantity=_required_float(order_raw.get("quantity"), "order.quantity"),
        price=_optional_float(order_raw.get("price"), "order.price"),
        stop_loss=_optional_float(order_raw.get("stop_loss"), "order.stop_loss"),
        take_profit=_optional_float(order_raw.get("take_profit"), "order.take_profit"),
        tag=_required_text(order_raw.get("tag", ""), "order.tag")
        if order_raw.get("tag", "")
        else "",
        setup_id=_optional_text(order_raw.get("setup_id")),
        client_order_id=_optional_text(order_raw.get("client_order_id")),
        status=_enum(OrderStatus, order_raw.get("status"), "order.status"),
        filled_quantity=_required_float(
            order_raw.get("filled_quantity", 0.0),
            "order.filled_quantity",
        ),
        remaining_quantity=_required_float(
            order_raw.get("remaining_quantity", 0.0),
            "order.remaining_quantity",
        ),
        average_fill_price=_optional_float(
            order_raw.get("average_fill_price"),
            "order.average_fill_price",
        ),
        fees_paid=_required_float(order_raw.get("fees_paid", 0.0), "order.fees_paid"),
        time_in_force=_enum(
            TimeInForce,
            order_raw.get("time_in_force", TimeInForce.GTC.value),
            "order.time_in_force",
        ),
        post_only=_required_bool(order_raw.get("post_only", False), "order.post_only"),
        reduce_only=_required_bool(order_raw.get("reduce_only", False), "order.reduce_only"),
        good_till_time=_optional_datetime(
            order_raw.get("good_till_time"),
            "order.good_till_time",
        ),
        trigger_price=_optional_float(
            order_raw.get("trigger_price"),
            "order.trigger_price",
        ),
        limit_price=_optional_float(order_raw.get("limit_price"), "order.limit_price"),
        triggered_at=_optional_datetime(
            order_raw.get("triggered_at"),
            "order.triggered_at",
        ),
        triggered_index=_optional_int(
            order_raw.get("triggered_index"),
            "order.triggered_index",
        ),
    )
    return OrderEvent(
        timestamp=_datetime_from_json(value.get("timestamp"), "order event timestamp"),
        event=_required_text(value.get("event"), "order event"),
        order=order,
        reason=_optional_text(value.get("reason")),
        event_id=_optional_text(value.get("event_id")),
        correlation_id=_optional_text(value.get("correlation_id")),
        causation_id=_optional_text(value.get("causation_id")),
    )


def _decode_trade(value: Mapping[str, Any]) -> Trade:
    return Trade(
        side=_enum(PositionSide, value.get("side"), "trade.side"),
        quantity=_required_float(value.get("quantity"), "trade.quantity"),
        entry_time=_datetime_from_json(value.get("entry_time"), "trade.entry_time"),
        exit_time=_datetime_from_json(value.get("exit_time"), "trade.exit_time"),
        entry_price=_required_float(value.get("entry_price"), "trade.entry_price"),
        exit_price=_required_float(value.get("exit_price"), "trade.exit_price"),
        gross_pnl=_required_float(value.get("gross_pnl"), "trade.gross_pnl"),
        fees=_required_float(value.get("fees"), "trade.fees"),
        net_pnl=_required_float(value.get("net_pnl"), "trade.net_pnl"),
        exit_reason=_required_text(value.get("exit_reason"), "trade.exit_reason"),
        tag=_required_text(value.get("tag", ""), "trade.tag") if value.get("tag", "") else "",
        initial_risk_amount=_optional_float(
            value.get("initial_risk_amount"),
            "trade.initial_risk_amount",
        ),
        r_multiple=_optional_float(value.get("r_multiple"), "trade.r_multiple"),
        holding_bars=_optional_int(value.get("holding_bars"), "trade.holding_bars"),
        setup_id=_optional_text(value.get("setup_id")),
        position_id=_optional_text(value.get("position_id")),
        entry_fill_count=_required_int(
            value.get("entry_fill_count", 1),
            "trade.entry_fill_count",
        ),
        exit_fill_count=_required_int(
            value.get("exit_fill_count", 1),
            "trade.exit_fill_count",
        ),
        position_closed=_required_bool(
            value.get("position_closed", True),
            "trade.position_closed",
        ),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value, "optional text")


def _optional_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _required_float(value, name)


def _optional_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _required_int(value, name)


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _datetime_from_json(value, name)
