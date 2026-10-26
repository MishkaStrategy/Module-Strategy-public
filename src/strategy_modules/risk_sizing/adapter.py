from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from types import MappingProxyType
from typing import Any, Mapping

from modular_core import (
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from ._canonical import fingerprint, manifest_number
from .calculator import RiskCalculator
from .config import RiskSizingConfig
from .models import RiskDecision, RiskDecisionStatus, RiskRejectReason, RiskRequest

FEATURE_REQUEST = "risk.request"
FEATURE_DECISION = "risk.decision"
FEATURE_SCHEMA_VERSION = "1"
STATE_SCHEMA_VERSION = "2"

PARAMETER_SCHEMA: Mapping[str, Mapping[str, Any]] = {
    "default_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.005,
    },
    "min_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.0001,
    },
    "max_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.02,
    },
    "max_trade_notional_multiple": {
        "type": "number",
        "minimum": 0.000001,
        "default": 3.0,
    },
    "max_portfolio_notional_multiple": {
        "type": "number",
        "minimum": 0.000001,
        "default": 5.0,
    },
    "max_symbol_notional_multiple": {
        "type": "number",
        "minimum": 0.000001,
        "default": 3.0,
    },
    "max_portfolio_risk_fraction": {
        "type": "number",
        "minimum": 0.000001,
        "maximum": 1,
        "default": 0.03,
    },
    "max_leverage": {
        "type": "number",
        "minimum": 0.000001,
        "default": 3.0,
    },
    "max_positions": {"type": "integer", "minimum": 1, "default": 10},
    "allow_risk_reduction": {"type": "boolean", "default": True},
    "require_price_alignment": {"type": "boolean", "default": True},
    "max_processed_requests": {"type": "integer", "minimum": 1, "default": 2048},
}


class RiskSizingModule(StrategyModule):
    """Deterministic Module API adapter for risk and position sizing."""

    name = "risk_sizing"
    manifest = ModuleManifest(
        name=name,
        version="1.1.0",
        api_version="1.0",
        required_features=(FEATURE_REQUEST,),
        produced_features=(FEATURE_DECISION,),
        parameters_schema=PARAMETER_SCHEMA,
        description="Fixed-fractional risk, rounding and portfolio/notional constraints.",
        state_schema_version=STATE_SCHEMA_VERSION,
        skip_until_ready=False,
    )

    def __init__(self, **parameters: Any) -> None:
        self.config = RiskSizingConfig.from_mapping(parameters)
        self.parameters = MappingProxyType(
            {
                "default_risk_fraction": manifest_number(
                    self.config.default_risk_fraction,
                    "default_risk_fraction",
                ),
                "min_risk_fraction": manifest_number(
                    self.config.min_risk_fraction,
                    "min_risk_fraction",
                ),
                "max_risk_fraction": manifest_number(
                    self.config.max_risk_fraction,
                    "max_risk_fraction",
                ),
                "max_trade_notional_multiple": manifest_number(
                    self.config.max_trade_notional_multiple,
                    "max_trade_notional_multiple",
                ),
                "max_portfolio_notional_multiple": manifest_number(
                    self.config.max_portfolio_notional_multiple,
                    "max_portfolio_notional_multiple",
                ),
                "max_symbol_notional_multiple": manifest_number(
                    self.config.max_symbol_notional_multiple,
                    "max_symbol_notional_multiple",
                ),
                "max_portfolio_risk_fraction": manifest_number(
                    self.config.max_portfolio_risk_fraction,
                    "max_portfolio_risk_fraction",
                ),
                "max_leverage": manifest_number(self.config.max_leverage, "max_leverage"),
                "max_positions": self.config.max_positions,
                "allow_risk_reduction": self.config.allow_risk_reduction,
                "require_price_alignment": self.config.require_price_alignment,
                "max_processed_requests": self.config.max_processed_requests,
            }
        )
        self._config_fingerprint = fingerprint(
            {
                "default_risk_fraction": self.config.default_risk_fraction,
                "min_risk_fraction": self.config.min_risk_fraction,
                "max_risk_fraction": self.config.max_risk_fraction,
                "max_trade_notional_multiple": self.config.max_trade_notional_multiple,
                "max_portfolio_notional_multiple": self.config.max_portfolio_notional_multiple,
                "max_symbol_notional_multiple": self.config.max_symbol_notional_multiple,
                "max_portfolio_risk_fraction": self.config.max_portfolio_risk_fraction,
                "max_leverage": self.config.max_leverage,
                "max_positions": self.config.max_positions,
                "allow_risk_reduction": self.config.allow_risk_reduction,
                "require_price_alignment": self.config.require_price_alignment,
                "max_processed_requests": self.config.max_processed_requests,
            }
        )
        self._calculator = RiskCalculator(self.config)
        self._request_fingerprints: dict[str, str] = {}
        self._request_order: deque[str] = deque()
        self._event_fingerprints: set[str] = set()
        self._event_order: deque[str] = deque()
        self._counters = self._empty_counters()
        self._stream_symbol: str | None = None

    @staticmethod
    def _empty_counters() -> dict[str, int]:
        return {
            "received": 0,
            "approved": 0,
            "rejected": 0,
            "duplicates": 0,
            "conflicts": 0,
            "invalid": 0,
            "stale": 0,
            "stream_changes": 0,
        }

    def evaluate(self, request: RiskRequest) -> RiskDecision:
        return self._calculator.evaluate(request)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.features is None:
            return self._skip("risk_request_unavailable")
        feature = view.features.latest(FEATURE_REQUEST, symbol=view.current.symbol)
        if feature is None:
            return self._skip("risk_request_unavailable")

        current_symbol = view.current.symbol
        if self._stream_symbol is None:
            self._stream_symbol = current_symbol
        elif self._stream_symbol != current_symbol:
            self._counters["stream_changes"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    f"stream symbol changed from {self._stream_symbol} to {current_symbol}",
                    RiskRejectReason.STREAM_CHANGED,
                ),
            )

        envelope = fingerprint(
            {
                "feature_id": feature.feature_id,
                "created_at": feature.created_at,
                "schema_version": feature.schema_version,
                "symbol": feature.symbol,
            }
        )
        try:
            event = fingerprint(
                {
                    "feature_id": feature.feature_id,
                    "created_at": feature.created_at,
                    "schema_version": feature.schema_version,
                    "symbol": feature.symbol,
                    "value": feature.value,
                }
            )
        except ValueError as exc:
            if envelope in self._event_fingerprints:
                self._counters["duplicates"] += 1
                return self._skip(
                    RiskRejectReason.DUPLICATE_REQUEST.value,
                    event="duplicate_invalid_event",
                )
            self._remember_event(envelope)
            self._counters["received"] += 1
            self._counters["invalid"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision("invalid", current_symbol, str(exc)),
            )
        if event in self._event_fingerprints:
            self._counters["duplicates"] += 1
            return self._skip(
                RiskRejectReason.DUPLICATE_REQUEST.value,
                event="duplicate_event",
            )
        self._remember_event(event)
        self._counters["received"] += 1

        current_time = view.current.close_time
        if feature.created_at > current_time:
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    "risk.request is from the future",
                    RiskRejectReason.FUTURE_REQUEST,
                ),
            )
        if feature.created_at < current_time:
            self._counters["stale"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    "risk.request is stale",
                    RiskRejectReason.STALE_REQUEST,
                ),
            )
        if feature.valid_until is not None and feature.valid_until < current_time:
            self._counters["stale"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    "risk.request is expired",
                    RiskRejectReason.STALE_REQUEST,
                ),
            )
        if feature.schema_version != FEATURE_SCHEMA_VERSION:
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    "unsupported risk.request feature schema",
                    RiskRejectReason.UNSUPPORTED_SCHEMA,
                ),
            )

        payload = feature.value
        if not isinstance(payload, Mapping):
            self._counters["invalid"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    "invalid",
                    current_symbol,
                    "risk.request payload must be a mapping",
                ),
            )

        request_id = str(payload.get("request_id", ""))
        payload_fingerprint = fingerprint(payload)
        previous = self._request_fingerprints.get(request_id) if request_id else None
        if previous == payload_fingerprint:
            self._counters["duplicates"] += 1
            return self._skip(
                RiskRejectReason.DUPLICATE_REQUEST.value,
                request_id=request_id,
                event="duplicate_request",
            )
        if previous is not None:
            self._counters["conflicts"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                RiskDecision(
                    request_id=request_id,
                    symbol=str(payload.get("symbol", current_symbol)),
                    status=RiskDecisionStatus.REJECTED,
                    reject_reason=RiskRejectReason.REQUEST_ID_CONFLICT,
                    message="request_id was reused with different payload",
                    setup_id=(
                        None if payload.get("setup_id") is None else str(payload["setup_id"])
                    ),
                ),
            )

        try:
            request = RiskRequest.from_mapping(payload)
            if request.symbol != current_symbol:
                raise ValueError("request symbol does not match current MarketView symbol")
            if feature.symbol not in {"*", request.symbol}:
                raise ValueError("feature symbol does not match request symbol")
            decision = self.evaluate(request)
        except (KeyError, TypeError, ValueError) as exc:
            self._counters["invalid"] += 1
            self._counters["rejected"] += 1
            return self._publish(
                view,
                self._invalid_decision(
                    request_id,
                    str(payload.get("symbol", current_symbol)),
                    str(exc),
                    setup_id=(
                        None if payload.get("setup_id") is None else str(payload["setup_id"])
                    ),
                ),
            )

        self._remember_request(request_id, payload_fingerprint)
        self._counters["approved" if decision.approved else "rejected"] += 1
        return self._publish(view, decision)

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": self._config_fingerprint,
            "stream_symbol": self._stream_symbol,
            "request_fingerprints": {
                key: self._request_fingerprints[key] for key in self._request_order
            },
            "request_order": list(self._request_order),
            "event_fingerprints": list(self._event_order),
            "event_order": list(self._event_order),
            "counters": dict(self._counters),
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not state:
            self._reset_state()
            return
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ValueError("unsupported risk sizing state schema_version")
        if state.get("config_fingerprint") != self._config_fingerprint:
            raise ValueError("risk sizing state configuration fingerprint mismatch")

        stream_symbol = state.get("stream_symbol")
        if stream_symbol is not None and (
            not isinstance(stream_symbol, str) or not stream_symbol.strip()
        ):
            raise ValueError("invalid risk sizing stream_symbol")
        request_fingerprints = state.get("request_fingerprints")
        request_order = state.get("request_order")
        event_fingerprints = state.get("event_fingerprints")
        event_order = state.get("event_order")
        counters = state.get("counters")
        if (
            not isinstance(request_fingerprints, Mapping)
            or isinstance(request_order, (str, bytes))
            or not isinstance(request_order, Sequence)
        ):
            raise ValueError("invalid risk sizing request state")
        if (
            isinstance(event_fingerprints, (str, bytes))
            or not isinstance(event_fingerprints, Sequence)
            or isinstance(event_order, (str, bytes))
            or not isinstance(event_order, Sequence)
        ):
            raise ValueError("invalid risk sizing event state")
        if not isinstance(counters, Mapping):
            raise ValueError("invalid risk sizing counter state")

        requests = {str(key): str(value) for key, value in request_fingerprints.items()}
        request_sequence = [str(value) for value in request_order]
        event_set = {str(value) for value in event_fingerprints}
        event_sequence = [str(value) for value in event_order]
        restored_counters: dict[str, int] = {}
        for key, value in counters.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("risk sizing counters must be non-negative integers")
            restored_counters[str(key)] = value

        self._validate_ordered_state(requests, request_sequence, "request")
        if len(event_sequence) != len(set(event_sequence)):
            raise ValueError("risk sizing event_order contains duplicates")
        if set(event_sequence) != event_set:
            raise ValueError("risk sizing event order and fingerprints disagree")
        if set(restored_counters) != set(self._counters) or any(
            value < 0 for value in restored_counters.values()
        ):
            raise ValueError("risk sizing counters are incompatible")

        limit = self.config.max_processed_requests
        request_sequence = request_sequence[-limit:]
        requests = {key: requests[key] for key in request_sequence}
        event_sequence = event_sequence[-limit:]

        self._stream_symbol = stream_symbol
        self._request_fingerprints = requests
        self._request_order = deque(request_sequence)
        self._event_fingerprints = set(event_sequence)
        self._event_order = deque(event_sequence)
        self._counters = restored_counters

    def _publish(self, view: MarketView, decision: RiskDecision) -> ModuleDecision:
        feature = Feature(
            key=FEATURE_DECISION,
            source_module=self.name,
            created_at=view.current.close_time,
            value=decision.to_mapping(),
            symbol=decision.symbol or view.current.symbol,
            timeframe=view.current.timeframe,
            valid_until=view.current.close_time,
            schema_version=FEATURE_SCHEMA_VERSION,
            invalidate_on_gap=False,
            metadata={"request_id": decision.request_id, "setup_id": decision.setup_id},
        )
        skips = (
            ()
            if decision.approved
            else ((decision.reject_reason or RiskRejectReason.INVALID_REQUEST).value,)
        )
        return ModuleDecision(
            features=(feature,),
            skips=skips,
            diagnostics=self._diagnostics(decision=decision),
        )

    def _skip(
        self,
        reason: str,
        *,
        request_id: str | None = None,
        event: str | None = None,
    ) -> ModuleDecision:
        return ModuleDecision(
            skips=(reason,),
            diagnostics=self._diagnostics(request_id=request_id, event=event),
        )

    def _diagnostics(
        self,
        *,
        decision: RiskDecision | None = None,
        request_id: str | None = None,
        event: str | None = None,
    ) -> Mapping[str, object]:
        values: dict[str, object] = {
            "counters": MappingProxyType(dict(self._counters)),
            "request_cache_size": len(self._request_order),
            "event_cache_size": len(self._event_order),
        }
        if request_id is not None:
            values["request_id"] = request_id
        if event is not None:
            values["event"] = event
        if decision is not None:
            values.update(
                {
                    "request_id": decision.request_id,
                    "status": decision.status.value,
                    "reject_reason": (
                        None if decision.reject_reason is None else decision.reject_reason.value
                    ),
                    "quantity": str(decision.quantity),
                    "notional": str(decision.notional),
                    "actual_risk_fraction": str(decision.actual_risk_fraction),
                }
            )
        return MappingProxyType(values)

    def _remember_request(self, request_id: str, value: str) -> None:
        self._request_fingerprints[request_id] = value
        self._request_order.append(request_id)
        while len(self._request_order) > self.config.max_processed_requests:
            removed = self._request_order.popleft()
            self._request_fingerprints.pop(removed, None)

    def _remember_event(self, value: str) -> None:
        self._event_fingerprints.add(value)
        self._event_order.append(value)
        while len(self._event_order) > self.config.max_processed_requests:
            removed = self._event_order.popleft()
            self._event_fingerprints.discard(removed)

    def _reset_state(self) -> None:
        self._stream_symbol = None
        self._request_fingerprints.clear()
        self._request_order.clear()
        self._event_fingerprints.clear()
        self._event_order.clear()
        self._counters = self._empty_counters()

    @staticmethod
    def _validate_ordered_state(
        values: Mapping[str, str],
        order: list[str],
        label: str,
    ) -> None:
        if len(order) != len(set(order)):
            raise ValueError(f"risk sizing {label}_order contains duplicates")
        if set(order) != set(values):
            raise ValueError(f"risk sizing {label} order and fingerprints disagree")

    @staticmethod
    def _invalid_decision(
        request_id: str,
        symbol: str,
        message: str,
        reason: RiskRejectReason = RiskRejectReason.INVALID_REQUEST,
        setup_id: str | None = None,
    ) -> RiskDecision:
        return RiskDecision(
            request_id=request_id or "invalid",
            symbol=symbol or "UNKNOWN",
            status=RiskDecisionStatus.REJECTED,
            reject_reason=reason,
            message=message,
            setup_id=setup_id,
        )
