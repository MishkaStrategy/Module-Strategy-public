from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from .enums import TradeAction
from .errors import DownstreamContractError
from .models import ExecutableTradePlan, ExecutionPrecondition
from .serialization import TradePlanSerializer


_HEX_DIGITS = frozenset("0123456789abcdef")


def _sha256(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise DownstreamContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DownstreamContractError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _serialized_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DownstreamContractError(f"serialized {name} must be UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DownstreamContractError(f"serialized {name} is not a valid datetime") from exc
    return _utc(parsed, name)


def _payload_digest(payload: str) -> str:
    return sha256(payload.encode("utf-8", errors="strict")).hexdigest()


@dataclass(frozen=True, slots=True)
class DownstreamPlanEnvelope:
    """Immutable handoff that preserves the canonical Router plan contract."""

    schema_version: str
    plan_id: str
    content_hash: str
    audit_hash: str
    action: TradeAction
    valid_from: datetime
    expires_at: datetime
    canonical_plan_json: str
    payload_sha256: str
    preconditions: tuple[ExecutionPrecondition, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise DownstreamContractError("unsupported downstream Router schema version")
        if not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise DownstreamContractError("plan_id must be a non-blank string")
        object.__setattr__(
            self,
            "content_hash",
            _sha256(self.content_hash, "content_hash"),
        )
        object.__setattr__(
            self,
            "audit_hash",
            _sha256(self.audit_hash, "audit_hash"),
        )
        object.__setattr__(
            self,
            "payload_sha256",
            _sha256(self.payload_sha256, "payload_sha256"),
        )
        if not isinstance(self.action, TradeAction):
            raise DownstreamContractError("action must be TradeAction")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))
        if self.expires_at <= self.valid_from:
            raise DownstreamContractError("expires_at must be after valid_from")
        if not isinstance(self.canonical_plan_json, str):
            raise DownstreamContractError("canonical_plan_json must be a string")
        try:
            actual_payload_hash = _payload_digest(self.canonical_plan_json)
        except UnicodeEncodeError as exc:
            raise DownstreamContractError("canonical_plan_json contains invalid Unicode") from exc
        if actual_payload_hash != self.payload_sha256:
            raise DownstreamContractError(
                "canonical Router plan payload SHA-256 does not match envelope"
            )
        if type(self.preconditions) is not tuple:
            raise DownstreamContractError("preconditions must be a tuple")
        if not self.preconditions or not all(
            isinstance(item, ExecutionPrecondition) for item in self.preconditions
        ):
            raise DownstreamContractError("preconditions must contain ExecutionPrecondition values")
        kinds = tuple(item.kind for item in self.preconditions)
        if len(set(kinds)) != len(kinds):
            raise DownstreamContractError("downstream precondition kinds must be unique")
        self._validate_canonical_payload()

    @classmethod
    def from_plan(
        cls,
        plan: ExecutableTradePlan,
        *,
        serializer: TradePlanSerializer | None = None,
        supported_actions: Collection[TradeAction] | None = None,
    ) -> DownstreamPlanEnvelope:
        if not isinstance(plan, ExecutableTradePlan):
            raise DownstreamContractError("plan must be ExecutableTradePlan")
        active_serializer = serializer or TradePlanSerializer()
        if not isinstance(active_serializer, TradePlanSerializer):
            raise DownstreamContractError("serializer must be TradePlanSerializer")
        canonical_plan_json = active_serializer.dumps_plan(plan)
        envelope = cls(
            schema_version=plan.schema_version,
            plan_id=plan.plan_id,
            content_hash=plan.content_hash,
            audit_hash=plan.audit_hash,
            action=plan.action,
            valid_from=plan.valid_from,
            expires_at=plan.expires_at,
            canonical_plan_json=canonical_plan_json,
            payload_sha256=_payload_digest(canonical_plan_json),
            preconditions=tuple(plan.preconditions),
        )
        if supported_actions is not None:
            envelope.require_supported_actions(supported_actions)
        return envelope

    def require_supported_actions(
        self,
        supported_actions: Collection[TradeAction],
    ) -> None:
        try:
            normalized = frozenset(supported_actions)
        except TypeError as exc:
            raise DownstreamContractError(
                "supported_actions must be a collection of TradeAction values"
            ) from exc
        if not normalized or not all(isinstance(item, TradeAction) for item in normalized):
            raise DownstreamContractError("supported_actions must contain TradeAction values")
        if self.action not in normalized:
            raise DownstreamContractError(
                f"downstream executor does not support action {self.action.value}"
            )

    def validate_at(self, timestamp: datetime) -> None:
        moment = _utc(timestamp, "timestamp")
        if moment < self.valid_from:
            raise DownstreamContractError("plan is not yet valid")
        if moment >= self.expires_at:
            raise DownstreamContractError("plan has expired")

    def precondition_mapping(self) -> Mapping[str, str]:
        return MappingProxyType({item.kind: item.expected for item in self.preconditions})

    def to_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "plan_id": self.plan_id,
                "content_hash": self.content_hash,
                "audit_hash": self.audit_hash,
                "action": self.action.value,
                "valid_from": self.valid_from,
                "expires_at": self.expires_at,
                "canonical_plan_json": self.canonical_plan_json,
                "payload_sha256": self.payload_sha256,
                "preconditions": tuple(
                    MappingProxyType(
                        {
                            "kind": item.kind,
                            "expected": item.expected,
                        }
                    )
                    for item in self.preconditions
                ),
            }
        )

    def _validate_canonical_payload(self) -> None:
        try:
            mapping = TradePlanSerializer().loads_plan_mapping(self.canonical_plan_json)
        except Exception as exc:
            raise DownstreamContractError(
                f"canonical Router plan payload is invalid: {exc}"
            ) from exc

        expected_scalars = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "content_hash": self.content_hash,
            "audit_hash": self.audit_hash,
            "action": self.action.value,
        }
        for name, expected_scalar in expected_scalars.items():
            if mapping.get(name) != expected_scalar:
                raise DownstreamContractError(
                    f"canonical Router plan {name} does not match envelope"
                )

        if _serialized_time(mapping.get("valid_from"), "valid_from") != self.valid_from:
            raise DownstreamContractError(
                "canonical Router plan valid_from does not match envelope"
            )
        if _serialized_time(mapping.get("expires_at"), "expires_at") != self.expires_at:
            raise DownstreamContractError(
                "canonical Router plan expires_at does not match envelope"
            )

        raw_preconditions = mapping.get("preconditions")
        if not isinstance(raw_preconditions, tuple):
            raise DownstreamContractError("canonical Router plan preconditions must be a tuple")
        serialized = tuple(
            (row.get("kind"), row.get("expected"))
            for row in raw_preconditions
            if isinstance(row, Mapping)
        )
        expected_preconditions = tuple((item.kind, item.expected) for item in self.preconditions)
        if len(serialized) != len(raw_preconditions) or serialized != expected_preconditions:
            raise DownstreamContractError(
                "canonical Router plan preconditions do not match envelope"
            )
