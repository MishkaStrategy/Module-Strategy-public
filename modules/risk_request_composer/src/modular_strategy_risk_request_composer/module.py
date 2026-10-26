from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from modular_core import Feature, MarketView, ModuleDecision, ModuleManifest, Side, StrategyModule
from strategy_modules.risk_sizing import (
    InstrumentRiskRules,
    PortfolioRiskSnapshot,
    RiskRequest,
)
from strategy_modules.risk_sizing.enums import TradeSide

MODULE_NAME = "risk_request_composer"
MODULE_VERSION = "0.1.0"
MODULE_API_VERSION = "1.0"
STATE_SCHEMA_VERSION = "1"
FEATURE_SCHEMA_VERSION = "1"
RISK_REQUEST_FEATURE = "risk.request"
DEFAULT_ENTRY_FEATURE = "entry_model.plan"
DEFAULT_PROTECTION_FEATURE = "protection_target.plan"
DEFAULT_INSTRUMENT_FEATURE = "instrument.rules"
DEFAULT_PORTFOLIO_FEATURE = "portfolio.risk_snapshot"
_MAX_CACHE = 100_000


class RiskRequestComposerError(ValueError):
    """Raised when trusted upstream features cannot form a safe risk request."""


def _text(value: object, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RiskRequestComposerError(f"{name} must be a non-blank trimmed string")
    if len(value) > maximum:
        raise RiskRequestComposerError(f"{name} exceeds maximum length")
    return value


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise RiskRequestComposerError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RiskRequestComposerError(f"{name} must be numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive finite" if positive else "finite"
        raise RiskRequestComposerError(f"{name} must be {qualifier}")
    return result


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise RiskRequestComposerError(f"{name} must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RiskRequestComposerError(f"{name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RiskRequestComposerError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RiskRequestComposerError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise RiskRequestComposerError(f"{name} keys must be strings")
    return value


def _entry_price(entry: Mapping[str, Any], current_close: Decimal) -> Decimal:
    order_type = _text(entry.get("order_type"), "entry.order_type")
    if order_type == "market":
        return current_close
    candidates = {
        "limit": entry.get("limit_price", entry.get("price")),
        "stop_market": entry.get("trigger_price"),
        "stop_limit": entry.get("limit_price", entry.get("price")),
    }
    if order_type not in candidates:
        raise RiskRequestComposerError(f"unsupported entry order_type: {order_type}")
    return _decimal(candidates[order_type], "entry price", positive=True)


class RiskRequestComposerModule(StrategyModule):
    """Compose one same-bar ``risk.request`` from typed entry/protection evidence.

    The module performs no sizing calculation and emits no order. Instrument and
    portfolio limits remain external facts; by default both are mandatory features.
    """

    name = MODULE_NAME

    def __init__(
        self,
        *,
        entry_feature_key: str = DEFAULT_ENTRY_FEATURE,
        protection_feature_key: str = DEFAULT_PROTECTION_FEATURE,
        instrument_feature_key: str = DEFAULT_INSTRUMENT_FEATURE,
        portfolio_feature_key: str = DEFAULT_PORTFOLIO_FEATURE,
        requested_risk_fraction: str | int | float | Decimal | None = None,
        require_instrument_feature: bool = True,
        require_portfolio_feature: bool = True,
        max_processed_requests: int = 4096,
    ) -> None:
        for name, value in (
            ("entry_feature_key", entry_feature_key),
            ("protection_feature_key", protection_feature_key),
            ("instrument_feature_key", instrument_feature_key),
            ("portfolio_feature_key", portfolio_feature_key),
        ):
            if "." not in _text(value, name):
                raise ValueError(f"{name} must be namespaced")
        if not isinstance(require_instrument_feature, bool) or not isinstance(
            require_portfolio_feature, bool
        ):
            raise TypeError("feature requirement flags must be bool")
        if (
            isinstance(max_processed_requests, bool)
            or not isinstance(max_processed_requests, int)
            or not 1 <= max_processed_requests <= _MAX_CACHE
        ):
            raise ValueError(f"max_processed_requests must be in [1, {_MAX_CACHE}]")
        risk_fraction = (
            None
            if requested_risk_fraction is None
            else _decimal(requested_risk_fraction, "requested_risk_fraction", positive=True)
        )
        if risk_fraction is not None and risk_fraction > Decimal("1"):
            raise ValueError("requested_risk_fraction cannot exceed 1")
        self.entry_feature_key = entry_feature_key
        self.protection_feature_key = protection_feature_key
        self.instrument_feature_key = instrument_feature_key
        self.portfolio_feature_key = portfolio_feature_key
        self.requested_risk_fraction = risk_fraction
        self.require_instrument_feature = require_instrument_feature
        self.require_portfolio_feature = require_portfolio_feature
        self.max_processed_requests = max_processed_requests
        self.parameters = MappingProxyType(
            {
                "entry_feature_key": entry_feature_key,
                "protection_feature_key": protection_feature_key,
                "instrument_feature_key": instrument_feature_key,
                "portfolio_feature_key": portfolio_feature_key,
                "requested_risk_fraction": ("" if risk_fraction is None else str(risk_fraction)),
                "require_instrument_feature": require_instrument_feature,
                "require_portfolio_feature": require_portfolio_feature,
                "max_processed_requests": max_processed_requests,
            }
        )
        required = [entry_feature_key, protection_feature_key]
        optional: list[str] = []
        for key, mandatory in (
            (instrument_feature_key, require_instrument_feature),
            (portfolio_feature_key, require_portfolio_feature),
        ):
            (required if mandatory else optional).append(key)
        self.manifest = ModuleManifest(
            name=MODULE_NAME,
            version=MODULE_VERSION,
            api_version=MODULE_API_VERSION,
            required_features=tuple(required),
            optional_features=tuple(optional),
            produced_features=(RISK_REQUEST_FEATURE,),
            parameters_schema={
                "entry_feature_key": {"type": "string", "default": DEFAULT_ENTRY_FEATURE},
                "protection_feature_key": {
                    "type": "string",
                    "default": DEFAULT_PROTECTION_FEATURE,
                },
                "instrument_feature_key": {
                    "type": "string",
                    "default": DEFAULT_INSTRUMENT_FEATURE,
                },
                "portfolio_feature_key": {
                    "type": "string",
                    "default": DEFAULT_PORTFOLIO_FEATURE,
                },
                "requested_risk_fraction": {"type": "string", "default": ""},
                "require_instrument_feature": {"type": "boolean", "default": True},
                "require_portfolio_feature": {"type": "boolean", "default": True},
                "max_processed_requests": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_CACHE,
                    "default": 4096,
                },
            },
            description=(
                "Deterministic same-bar adapter from EntryPlan and ProtectionPlan "
                "to RiskSizing risk.request."
            ),
            state_schema_version=STATE_SCHEMA_VERSION,
            skip_until_ready=False,
        )
        self._processed: dict[str, str] = {}
        self._stream: tuple[str, str] | None = None

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not view.base_bars:
            return self._skip("risk_request_composer.empty_view")
        current = view.current
        stream = (current.symbol, current.timeframe)
        if self._stream is None:
            self._stream = stream
        elif stream != self._stream:
            raise RiskRequestComposerError("module instance cannot change symbol/timeframe stream")
        if view.data_gap:
            return self._skip("risk_request_composer.data_gap")
        if view.features is None:
            return self._skip("risk_request_composer.no_feature_view")
        entry_feature = view.features.latest(
            self.entry_feature_key,
            symbol=current.symbol,
            source_module="entry_model",
        )
        protection_feature = view.features.latest(
            self.protection_feature_key,
            symbol=current.symbol,
            source_module="protection_target",
        )
        if entry_feature is None or protection_feature is None:
            return self._skip("risk_request_composer.upstream_not_ready")
        try:
            request = self._compose(view, entry_feature, protection_feature)
        except RiskRequestComposerError as exc:
            return self._skip(
                "risk_request_composer.invalid_upstream",
                error=str(exc),
            )
        payload = request.to_mapping()
        fingerprint = _sha256(payload)
        existing = self._processed.get(request.request_id)
        if existing is not None:
            if existing != fingerprint:
                raise RiskRequestComposerError(
                    "request_id was already observed with different content"
                )
            return self._skip("risk_request_composer.duplicate_request")
        if len(self._processed) >= self.max_processed_requests:
            raise RiskRequestComposerError("processed request cache limit exceeded")
        feature = Feature(
            key=RISK_REQUEST_FEATURE,
            source_module=MODULE_NAME,
            created_at=current.close_time,
            value=payload,
            symbol=current.symbol,
            timeframe=current.timeframe,
            valid_until=current.close_time,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=f"risk-request-{request.request_id}",
            invalidate_on_gap=True,
            metadata={
                "entry_feature_id": entry_feature.feature_id,
                "protection_feature_id": protection_feature.feature_id,
                "payload_sha256": fingerprint,
            },
        )
        self._processed[request.request_id] = fingerprint
        return ModuleDecision(
            features=(feature,),
            diagnostics={
                "state": "risk_request_created",
                "request_id": request.request_id,
                "processed_count": len(self._processed),
            },
        )

    def _compose(
        self, view: MarketView, entry_feature: Feature, protection_feature: Feature
    ) -> RiskRequest:
        current = view.current
        entry = _mapping(entry_feature.value, "entry plan")
        protection = _mapping(protection_feature.value, "protection plan")
        if str(entry.get("schema_version")) != "2":
            raise RiskRequestComposerError("unsupported EntryPlan schema")
        if str(protection.get("schema_version")) != "1":
            raise RiskRequestComposerError("unsupported ProtectionPlan schema")
        plan_id = _text(entry.get("plan_id"), "entry.plan_id")
        protection_plan_id = _text(
            protection.get("protection_plan_id"), "protection.protection_plan_id"
        )
        if _text(protection.get("entry_plan_id"), "protection.entry_plan_id") != plan_id:
            raise RiskRequestComposerError("protection is bound to another entry plan")
        setup_id = _text(entry.get("setup_id"), "entry.setup_id")
        if _text(protection.get("setup_id"), "protection.setup_id") != setup_id:
            raise RiskRequestComposerError("entry/protection setup_id mismatch")
        symbol = _text(entry.get("symbol"), "entry.symbol")
        if (
            symbol != current.symbol
            or _text(protection.get("symbol"), "protection.symbol") != symbol
        ):
            raise RiskRequestComposerError("entry/protection/current symbol mismatch")
        side_text = _text(entry.get("side"), "entry.side")
        if _text(protection.get("side"), "protection.side") != side_text:
            raise RiskRequestComposerError("entry/protection side mismatch")
        if side_text not in {Side.BUY.value, Side.SELL.value}:
            raise RiskRequestComposerError("unsupported side")
        created_at = _utc(entry.get("created_at"), "entry.created_at")
        protection_created_at = _utc(protection.get("created_at"), "protection.created_at")
        if created_at != current.close_time or protection_created_at != current.close_time:
            raise RiskRequestComposerError("upstream plans must be created on current closed bar")
        expires_at = _utc(entry.get("expires_at"), "entry.expires_at")
        if expires_at <= current.close_time:
            raise RiskRequestComposerError("entry plan is expired")
        valid_until_raw = protection.get("valid_until")
        if (
            valid_until_raw is not None
            and _utc(valid_until_raw, "protection.valid_until") < current.close_time
        ):
            raise RiskRequestComposerError("protection plan is expired")
        entry_price = _entry_price(entry, _decimal(current.close, "current close", positive=True))
        protection_entry = _decimal(
            protection.get("entry_price"), "protection.entry_price", positive=True
        )
        if protection_entry != entry_price:
            raise RiskRequestComposerError("entry/protection price mismatch")
        stop_loss = _decimal(protection.get("stop_loss"), "stop_loss", positive=True)
        instrument = self._instrument(view)
        portfolio = self._portfolio(view)
        request_id = _sha256(
            {
                "schema_version": FEATURE_SCHEMA_VERSION,
                "entry_plan_id": plan_id,
                "protection_plan_id": protection_plan_id,
                "equity": str(_decimal(view.equity, "equity", positive=True)),
                "instrument": instrument.to_mapping(),
                "portfolio": portfolio.to_mapping(),
                "requested_risk_fraction": (
                    None
                    if self.requested_risk_fraction is None
                    else str(self.requested_risk_fraction)
                ),
            }
        )
        return RiskRequest(
            request_id=f"rr-{request_id[:40]}",
            symbol=symbol,
            side=TradeSide.BUY if side_text == "buy" else TradeSide.SELL,
            entry_price=entry_price,
            stop_loss=stop_loss,
            equity=_decimal(view.equity, "equity", positive=True),
            instrument=instrument,
            portfolio=portfolio,
            requested_risk_fraction=self.requested_risk_fraction,
            available_margin=_decimal(view.cash, "cash") if view.cash >= 0 else None,
            setup_id=setup_id,
            metadata={
                "entry_plan_id": plan_id,
                "protection_plan_id": protection_plan_id,
                "entry_feature_id": entry_feature.feature_id,
                "protection_feature_id": protection_feature.feature_id,
                "entry_price_source": (
                    "bar_close" if entry.get("order_type") == "market" else "entry_plan"
                ),
            },
        )

    def _instrument(self, view: MarketView) -> InstrumentRiskRules:
        assert view.features is not None
        feature = view.features.latest(
            self.instrument_feature_key,
            symbol=view.current.symbol,
        )
        if feature is None:
            if self.require_instrument_feature:
                raise RiskRequestComposerError("instrument rules feature is required")
            return InstrumentRiskRules(quantity_step=Decimal("0.00000001"))
        try:
            return InstrumentRiskRules.from_mapping(_mapping(feature.value, "instrument rules"))
        except (ValueError, KeyError) as exc:
            raise RiskRequestComposerError("invalid instrument rules") from exc

    def _portfolio(self, view: MarketView) -> PortfolioRiskSnapshot:
        assert view.features is not None
        feature = view.features.latest(
            self.portfolio_feature_key,
            symbol=view.current.symbol,
        )
        if feature is None:
            if self.require_portfolio_feature:
                raise RiskRequestComposerError("portfolio risk snapshot feature is required")
            return PortfolioRiskSnapshot()
        try:
            return PortfolioRiskSnapshot.from_mapping(
                _mapping(feature.value, "portfolio risk snapshot")
            )
        except ValueError as exc:
            raise RiskRequestComposerError("invalid portfolio risk snapshot") from exc

    def snapshot_state(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "config_fingerprint": _sha256(dict(self.parameters)),
            "stream": self._stream,
            "processed": dict(sorted(self._processed.items())),
        }
        payload["integrity_hash"] = _sha256(payload)
        return MappingProxyType(payload)

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not state:
            self._processed = {}
            self._stream = None
            return
        payload = dict(state)
        stored = payload.pop("integrity_hash", None)
        if not isinstance(stored, str) or _sha256(payload) != stored:
            raise RiskRequestComposerError("state integrity check failed")
        if payload.get("schema_version") != STATE_SCHEMA_VERSION:
            raise RiskRequestComposerError("state schema is incompatible")
        if payload.get("config_fingerprint") != _sha256(dict(self.parameters)):
            raise RiskRequestComposerError("state configuration is incompatible")
        raw_stream = payload.get("stream")
        if raw_stream is None:
            stream = None
        elif (
            isinstance(raw_stream, (list, tuple))
            and len(raw_stream) == 2
            and all(isinstance(item, str) and item for item in raw_stream)
        ):
            stream = (raw_stream[0], raw_stream[1])
        else:
            raise RiskRequestComposerError("state stream is invalid")
        processed = payload.get("processed")
        if not isinstance(processed, Mapping) or len(processed) > self.max_processed_requests:
            raise RiskRequestComposerError("state processed cache is invalid")
        normalized: dict[str, str] = {}
        for key, value in processed.items():
            request_id = _text(key, "processed request_id")
            digest = _text(value, "processed payload hash")
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise RiskRequestComposerError("processed payload hash is invalid")
            normalized[request_id] = digest
        self._stream = stream
        self._processed = normalized

    @staticmethod
    def _skip(reason: str, **diagnostics: object) -> ModuleDecision:
        return ModuleDecision(
            skips=(reason,),
            diagnostics={"state": "skipped", "reason": reason, **diagnostics},
        )
