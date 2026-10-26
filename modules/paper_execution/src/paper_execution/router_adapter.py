from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from modular_core import OrderType, PositionSide, Side

from ._utils import canonical_json, sha256_text, utc
from .contracts import ExecutableTradePlan, ExecutionIntent, StateVersions
from .enums import IntentRole, PaperReasonCode, PlanAction
from .exceptions import PlanValidationError

_SUPPORTED_ROUTER_ACTIONS = frozenset(
    {"open_position", "add_to_position", "reduce_position", "close_position"}
)
_HEX_DIGITS = frozenset("0123456789abcdef")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a non-blank string",
        )
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a lowercase SHA-256 digest",
        )
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a mapping",
        )
    return value


def _sequence(value: object, name: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be an immutable tuple",
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be boolean",
        )
    return value


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a positive integer",
        )
    return value


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be a canonical decimal string",
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} is not a valid decimal",
        ) from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} is outside the supported decimal domain",
        )
    return result


def _optional_decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> Decimal | None:
    return None if value is None else _decimal(value, name, positive=positive)


def _serialized_time(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be canonical UTC text",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PlanValidationError(
            PaperReasonCode.INVALID_PLAN,
            f"Router {name} must be canonical UTC text",
        ) from exc
    return utc(parsed, f"Router {name}")


@dataclass(frozen=True, slots=True)
class RouterPlanEvidence:
    schema_version: str
    plan_id: str
    content_hash: str
    audit_hash: str
    payload_sha256: str
    action: str
    portfolio_id: str
    instrument: str
    valid_from: datetime
    expires_at: datetime
    canonical_plan_json: str
    preconditions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise PlanValidationError(
                PaperReasonCode.UNSUPPORTED_PLAN_SCHEMA,
                "unsupported Router envelope schema",
            )
        for name in ("plan_id", "action", "portfolio_id", "instrument"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        for name in ("content_hash", "audit_hash", "payload_sha256"):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        object.__setattr__(self, "valid_from", utc(self.valid_from, "Router valid_from"))
        object.__setattr__(self, "expires_at", utc(self.expires_at, "Router expires_at"))
        if self.expires_at <= self.valid_from:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router validity interval is invalid",
            )
        if not isinstance(self.canonical_plan_json, str):
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router canonical plan payload must be text",
            )
        if sha256_text(self.canonical_plan_json) != self.payload_sha256:
            raise PlanValidationError(
                PaperReasonCode.PLAN_HASH_MISMATCH,
                "Router canonical payload SHA-256 mismatch",
            )
        if type(self.preconditions) is not tuple or not self.preconditions:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router preconditions must be a non-empty tuple",
            )
        normalized: list[tuple[str, str]] = []
        for index, item in enumerate(self.preconditions):
            if type(item) is not tuple or len(item) != 2:
                raise PlanValidationError(
                    PaperReasonCode.INVALID_PLAN,
                    f"Router precondition {index} is invalid",
                )
            normalized.append(
                (
                    _identifier(item[0], f"preconditions[{index}].kind"),
                    _identifier(item[1], f"preconditions[{index}].expected"),
                )
            )
        if len({kind for kind, _ in normalized}) != len(normalized):
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router precondition kinds must be unique",
            )
        object.__setattr__(self, "preconditions", tuple(normalized))

    @property
    def fingerprint(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "schema_version": self.schema_version,
                    "plan_id": self.plan_id,
                    "content_hash": self.content_hash,
                    "audit_hash": self.audit_hash,
                    "payload_sha256": self.payload_sha256,
                    "action": self.action,
                    "portfolio_id": self.portfolio_id,
                    "instrument": self.instrument,
                    "valid_from": self.valid_from,
                    "expires_at": self.expires_at,
                    "canonical_plan_json": self.canonical_plan_json,
                    "preconditions": self.preconditions,
                }
            )
        )

    def journal_payload(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "plan_id": self.plan_id,
                "content_hash": self.content_hash,
                "audit_hash": self.audit_hash,
                "payload_sha256": self.payload_sha256,
                "action": self.action,
                "portfolio_id": self.portfolio_id,
                "instrument": self.instrument,
                "valid_from": self.valid_from,
                "expires_at": self.expires_at,
                "canonical_plan_json": self.canonical_plan_json,
                "preconditions": [
                    {"kind": kind, "expected": expected} for kind, expected in self.preconditions
                ],
                "evidence_fingerprint": self.fingerprint,
            }
        )

    @classmethod
    def from_journal_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> RouterPlanEvidence:
        raw_preconditions = payload.get("preconditions")
        if not isinstance(raw_preconditions, tuple):
            raise ValueError("Router journal preconditions must be a tuple")
        preconditions: list[tuple[str, str]] = []
        for index, raw in enumerate(raw_preconditions):
            row = _mapping(raw, f"journal preconditions[{index}]")
            preconditions.append(
                (
                    _identifier(row.get("kind"), "journal precondition kind"),
                    _identifier(
                        row.get("expected"),
                        "journal precondition expected",
                    ),
                )
            )
        valid_from = payload.get("valid_from")
        expires_at = payload.get("expires_at")
        if not isinstance(valid_from, datetime) or not isinstance(expires_at, datetime):
            raise ValueError("Router journal validity fields must be datetimes")
        evidence = cls(
            schema_version=_identifier(
                payload.get("schema_version"),
                "journal schema_version",
            ),
            plan_id=_identifier(payload.get("plan_id"), "journal plan_id"),
            content_hash=_digest(
                payload.get("content_hash"),
                "journal content_hash",
            ),
            audit_hash=_digest(payload.get("audit_hash"), "journal audit_hash"),
            payload_sha256=_digest(
                payload.get("payload_sha256"),
                "journal payload_sha256",
            ),
            action=_identifier(payload.get("action"), "journal action"),
            portfolio_id=_identifier(
                payload.get("portfolio_id"),
                "journal portfolio_id",
            ),
            instrument=_identifier(
                payload.get("instrument"),
                "journal instrument",
            ),
            valid_from=valid_from,
            expires_at=expires_at,
            canonical_plan_json=_identifier(
                payload.get("canonical_plan_json"),
                "journal canonical_plan_json",
            ),
            preconditions=tuple(preconditions),
        )
        if payload.get("evidence_fingerprint") != evidence.fingerprint:
            raise ValueError("Router journal evidence fingerprint mismatch")
        return evidence


@dataclass(frozen=True, slots=True)
class RouterOCCSnapshot:
    portfolio_id: str
    instrument: str
    observed_at: datetime
    values: Mapping[str, str]
    source: str

    def __post_init__(self) -> None:
        for name in ("portfolio_id", "instrument", "source"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"OCC {name}"))
        object.__setattr__(self, "observed_at", utc(self.observed_at, "OCC observed_at"))
        if not isinstance(self.values, Mapping):
            raise PlanValidationError(
                PaperReasonCode.STATE_VERSION_MISMATCH,
                "Router OCC values must be a mapping",
            )
        normalized = {
            _identifier(kind, "OCC kind"): _identifier(expected, f"OCC[{kind!r}]")
            for kind, expected in self.values.items()
        }
        object.__setattr__(self, "values", MappingProxyType(normalized))

    def validate_for(
        self,
        *,
        evidence: RouterPlanEvidence,
        timestamp: datetime,
    ) -> None:
        if (
            self.portfolio_id != evidence.portfolio_id
            or self.instrument != evidence.instrument
            or self.observed_at != utc(timestamp, "Router OCC timestamp")
        ):
            raise PlanValidationError(
                PaperReasonCode.STATE_VERSION_MISMATCH,
                "Router OCC snapshot scope or timestamp mismatch",
                retryable=True,
            )
        if dict(self.values) != dict(evidence.preconditions):
            raise PlanValidationError(
                PaperReasonCode.STATE_VERSION_MISMATCH,
                "Router OCC evidence is missing, stale or mismatched",
                retryable=True,
            )


@runtime_checkable
class RouterOCCProvider(Protocol):
    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot: ...


@dataclass(frozen=True, slots=True)
class RouterAdaptation:
    evidence: RouterPlanEvidence
    plan: ExecutableTradePlan


@dataclass(frozen=True, slots=True)
class _RouterRuntime:
    envelope_type: Any
    serializer_type: Any
    trade_action_type: Any


def _router_runtime() -> _RouterRuntime:
    try:
        module = import_module("modular_strategy_trade_plan_router")
    except ImportError as exc:
        raise PlanValidationError(
            PaperReasonCode.INVALID_CONFIGURATION,
            "canonical Decision Router package is unavailable",
        ) from exc
    envelope_type = getattr(module, "DownstreamPlanEnvelope", None)
    serializer_type = getattr(module, "TradePlanSerializer", None)
    trade_action_type = getattr(module, "TradeAction", None)
    if not all(
        isinstance(value, type) for value in (envelope_type, serializer_type, trade_action_type)
    ):
        raise PlanValidationError(
            PaperReasonCode.INVALID_CONFIGURATION,
            "canonical Decision Router public contract is incomplete",
        )
    return _RouterRuntime(envelope_type, serializer_type, trade_action_type)


class RouterPlanAdapter:
    def inspect(
        self,
        envelope: object,
        *,
        now: datetime,
    ) -> tuple[RouterPlanEvidence, Mapping[str, Any]]:
        runtime = _router_runtime()
        if not isinstance(envelope, runtime.envelope_type):
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "input must be the canonical Router DownstreamPlanEnvelope",
            )
        moment = utc(now, "Router validation time")
        valid_from = utc(envelope.valid_from, "Router valid_from")
        expires_at = utc(envelope.expires_at, "Router expires_at")
        if moment < valid_from:
            raise PlanValidationError(
                PaperReasonCode.PLAN_NOT_YET_VALID,
                "Router envelope is not yet valid",
                retryable=True,
            )
        if moment >= expires_at:
            raise PlanValidationError(
                PaperReasonCode.STALE_PLAN,
                "Router envelope has expired",
            )
        try:
            rebuilt = runtime.envelope_type(
                schema_version=envelope.schema_version,
                plan_id=envelope.plan_id,
                content_hash=envelope.content_hash,
                audit_hash=envelope.audit_hash,
                action=envelope.action,
                valid_from=envelope.valid_from,
                expires_at=envelope.expires_at,
                canonical_plan_json=envelope.canonical_plan_json,
                payload_sha256=envelope.payload_sha256,
                preconditions=envelope.preconditions,
            )
            supported_actions = {
                runtime.trade_action_type(value) for value in _SUPPORTED_ROUTER_ACTIONS
            }
            rebuilt.require_supported_actions(supported_actions)
            rebuilt.validate_at(moment)
            mapping = runtime.serializer_type().loads_plan_mapping(rebuilt.canonical_plan_json)
        except PlanValidationError:
            raise
        except Exception as exc:
            action = getattr(getattr(envelope, "action", None), "value", None)
            text = str(exc).lower()
            if action not in _SUPPORTED_ROUTER_ACTIONS:
                reason = PaperReasonCode.UNSUPPORTED_PLAN_ACTION
            elif "hash" in text or "payload" in text or "canonical" in text:
                reason = PaperReasonCode.PLAN_HASH_MISMATCH
            else:
                reason = PaperReasonCode.INVALID_PLAN
            raise PlanValidationError(
                reason,
                f"Router envelope validation failed: {exc}",
            ) from exc
        plan_mapping = _mapping(mapping, "canonical plan")
        raw_preconditions = rebuilt.preconditions
        preconditions = tuple(
            (
                _identifier(item.kind, "precondition kind"),
                _identifier(item.expected, "precondition expected"),
            )
            for item in raw_preconditions
        )
        evidence = RouterPlanEvidence(
            schema_version=_identifier(rebuilt.schema_version, "schema_version"),
            plan_id=_identifier(rebuilt.plan_id, "plan_id"),
            content_hash=_digest(rebuilt.content_hash, "content_hash"),
            audit_hash=_digest(rebuilt.audit_hash, "audit_hash"),
            payload_sha256=_digest(rebuilt.payload_sha256, "payload_sha256"),
            action=_identifier(rebuilt.action.value, "action"),
            portfolio_id=_identifier(plan_mapping.get("portfolio_id"), "portfolio_id"),
            instrument=_identifier(plan_mapping.get("symbol"), "symbol"),
            valid_from=valid_from,
            expires_at=expires_at,
            canonical_plan_json=_identifier(
                rebuilt.canonical_plan_json,
                "canonical_plan_json",
            ),
            preconditions=preconditions,
        )
        return evidence, plan_mapping

    def adapt(
        self,
        envelope: object,
        *,
        now: datetime,
        paper_versions: StateVersions,
        metadata_version: str,
        paper_configuration_hash: str,
        occ_snapshot: RouterOCCSnapshot,
    ) -> RouterAdaptation:
        evidence, mapping = self.inspect(envelope, now=now)
        occ_snapshot.validate_for(evidence=evidence, timestamp=now)
        return RouterAdaptation(
            evidence=evidence,
            plan=self._translate(
                evidence=evidence,
                mapping=mapping,
                paper_versions=paper_versions,
                metadata_version=metadata_version,
                paper_configuration_hash=paper_configuration_hash,
            ),
        )

    def _translate(
        self,
        *,
        evidence: RouterPlanEvidence,
        mapping: Mapping[str, Any],
        paper_versions: StateVersions,
        metadata_version: str,
        paper_configuration_hash: str,
    ) -> ExecutableTradePlan:
        action = PlanAction(evidence.action)
        router_side = _identifier(mapping.get("side"), "side")
        if router_side not in {"long", "short"}:
            raise PlanValidationError(
                PaperReasonCode.INVALID_PLAN,
                "Router side is unsupported",
            )
        position_side = PositionSide.LONG if router_side == "long" else PositionSide.SHORT
        entry_side = Side.BUY if router_side == "long" else Side.SELL
        if action in {PlanAction.REDUCE_POSITION, PlanAction.CLOSE_POSITION}:
            entry_side = entry_side.opposite
        if _sequence(mapping.get("cancellation_intents"), "cancellation_intents"):
            raise PlanValidationError(
                PaperReasonCode.UNSUPPORTED_PLAN_ACTION,
                "Router cancellation sequencing is not supported by this adapter checkpoint",
            )

        entry = _mapping(mapping.get("entry_intent"), "entry_intent")
        quantity = _decimal(entry.get("quantity"), "entry_intent.quantity", positive=True)
        limit_price = _optional_decimal(
            entry.get("limit_price"),
            "entry_intent.limit_price",
            positive=True,
        ) or _optional_decimal(entry.get("price"), "entry_intent.price", positive=True)
        trigger_price = _optional_decimal(
            entry.get("trigger_price"),
            "entry_intent.trigger_price",
            positive=True,
        )
        sequencing_rules = tuple(
            _identifier(value, "sequencing_rule")
            for value in _sequence(mapping.get("sequencing_rules"), "sequencing_rules")
        )
        position_raw = mapping.get("position_intent")
        position_id = None
        if position_raw is not None:
            position_id = _identifier(
                _mapping(position_raw, "position_intent").get("position_id"),
                "position_intent.position_id",
            )
        common_metadata: dict[str, Any] = {
            "router_plan_id": evidence.plan_id,
            "router_content_hash": evidence.content_hash,
            "router_audit_hash": evidence.audit_hash,
            "router_payload_sha256": evidence.payload_sha256,
            "router_action": evidence.action,
            "router_side": router_side,
            "router_position_id": position_id,
            "router_sequencing_rules": sequencing_rules,
        }
        entry_lifecycle_metadata: dict[str, Any] = {}
        protection_raw = mapping.get("protection_intent")
        if protection_raw is not None:
            lifecycle = _mapping(protection_raw, "protection_intent")
            max_holding_seconds = lifecycle.get("max_holding_seconds")
            if max_holding_seconds is not None:
                entry_lifecycle_metadata["router_max_holding_seconds"] = _positive_integer(
                    max_holding_seconds,
                    "protection_intent.max_holding_seconds",
                )
            raw_no_progress = lifecycle.get("no_progress_rule")
            if raw_no_progress is not None:
                no_progress = _mapping(
                    raw_no_progress,
                    "protection_intent.no_progress_rule",
                )
                if no_progress:
                    entry_lifecycle_metadata["router_no_progress_after_seconds"] = (
                        _positive_integer(
                            no_progress.get("after_seconds"),
                            "protection_intent.no_progress_rule.after_seconds",
                        )
                    )
                    entry_lifecycle_metadata["router_no_progress_minimum_mfe_r"] = str(
                        _decimal(
                            no_progress.get("minimum_mfe_r"),
                            "protection_intent.no_progress_rule.minimum_mfe_r",
                            positive=True,
                        )
                    )

        intents: list[ExecutionIntent] = [
            ExecutionIntent(
                intent_id="router-entry",
                role=IntentRole.ENTRY,
                side=entry_side,
                order_type=self._order_type(entry.get("order_type")),
                quantity=None if action is PlanAction.CLOSE_POSITION else quantity,
                limit_price=limit_price,
                trigger_price=trigger_price,
                post_only=_boolean(entry.get("post_only"), "entry_intent.post_only"),
                reduce_only=_boolean(entry.get("reduce_only"), "entry_intent.reduce_only"),
                close_position=action is PlanAction.CLOSE_POSITION,
                position_side=position_side,
                metadata={
                    **common_metadata,
                    "router_role": "entry",
                    "router_reference_entry_price": str(
                        _decimal(
                            entry.get("reference_entry_price"),
                            "entry_intent.reference_entry_price",
                            positive=True,
                        )
                    ),
                    "router_ttl_bars": entry.get("ttl_bars"),
                    **entry_lifecycle_metadata,
                },
            )
        ]

        if protection_raw is not None:
            protection = _mapping(protection_raw, "protection_intent")
            targets = _sequence(protection.get("targets"), "protection_intent.targets")
            if len(targets) != 1:
                raise PlanValidationError(
                    PaperReasonCode.UNSUPPORTED_PLAN_ACTION,
                    "Paper deferred protection currently supports exactly one Router target",
                )
            target = _mapping(targets[0], "protection_intent.targets[0]")
            allocation = _decimal(target.get("allocation"), "target allocation", positive=True)
            if allocation != Decimal("1"):
                raise PlanValidationError(
                    PaperReasonCode.UNSUPPORTED_PLAN_ACTION,
                    "Paper deferred protection does not yet support partial target allocation",
                )
            exit_side = Side.SELL if router_side == "long" else Side.BUY
            oco_group_id = f"router-oco-{evidence.fingerprint[:24]}"
            intents.append(
                ExecutionIntent(
                    intent_id="router-stop",
                    role=IntentRole.STOP,
                    side=exit_side,
                    order_type=OrderType.STOP_MARKET,
                    quantity=quantity,
                    trigger_price=_decimal(
                        protection.get("stop_loss"),
                        "protection_intent.stop_loss",
                        positive=True,
                    ),
                    reduce_only=True,
                    position_side=position_side,
                    oco_group_id=oco_group_id,
                    metadata={**common_metadata, "router_role": "stop"},
                )
            )
            target_price = _decimal(target.get("price"), "target price", positive=True)
            target_type = _identifier(target.get("target_type"), "target_type")
            if target_type == "limit":
                target_order_type = OrderType.TAKE_PROFIT_LIMIT
                target_limit_price = target_price
            elif target_type == "market":
                target_order_type = OrderType.TAKE_PROFIT_MARKET
                target_limit_price = None
            else:
                raise PlanValidationError(
                    PaperReasonCode.UNSUPPORTED_PAPER_ORDER_TYPE,
                    f"unsupported Router target type: {target_type}",
                )
            intents.append(
                ExecutionIntent(
                    intent_id="router-target-0000",
                    role=IntentRole.TARGET,
                    side=exit_side,
                    order_type=target_order_type,
                    quantity=quantity,
                    limit_price=target_limit_price,
                    trigger_price=target_price,
                    reduce_only=True,
                    position_side=position_side,
                    target_index=0,
                    oco_group_id=oco_group_id,
                    metadata={
                        **common_metadata,
                        "router_role": "target",
                        "router_target_allocation": str(allocation),
                        "router_target_type": target_type,
                    },
                )
            )

        input_hashes = {
            f"router_input_{index:04d}": _digest(value, "input_hash")
            for index, value in enumerate(_sequence(mapping.get("input_hashes"), "input_hashes"))
        }
        return ExecutableTradePlan.create(
            plan_id=evidence.plan_id,
            created_at=_serialized_time(mapping.get("created_at"), "created_at"),
            valid_from=evidence.valid_from,
            expires_at=evidence.expires_at,
            portfolio_id=evidence.portfolio_id,
            instrument=evidence.instrument,
            market=_identifier(mapping.get("market"), "market"),
            action=action,
            intents=tuple(intents),
            expected_versions=paper_versions,
            metadata_version=_identifier(metadata_version, "metadata_version"),
            configuration_hash=_digest(
                paper_configuration_hash,
                "Paper configuration_hash",
            ),
            input_hashes=input_hashes,
        )

    @staticmethod
    def _order_type(value: object) -> OrderType:
        name = _identifier(value, "entry_intent.order_type")
        mapping = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop_market": OrderType.STOP_MARKET,
            "stop_limit": OrderType.STOP_LIMIT,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise PlanValidationError(
                PaperReasonCode.UNSUPPORTED_PAPER_ORDER_TYPE,
                f"unsupported Router entry order type: {name}",
            ) from exc
