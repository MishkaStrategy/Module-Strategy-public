from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from ._utils import canonical_json, decimal, sha256_text
from .enums import IntentRole, PaperReasonCode
from .exceptions import OrderStateError


class ProtectionActivationPolicy(str, Enum):
    AFTER_FIRST_FILL_PROPORTIONAL = "after_first_fill_proportional"
    AFTER_FULL_FILL = "after_full_fill"
    AFTER_ENTRY_TERMINAL = "after_entry_terminal"


@dataclass(frozen=True, slots=True)
class DeferredProtectionLeg:
    client_order_id: str
    role: IntentRole
    planned_quantity: Decimal
    trigger_price: Decimal | None = None
    activation_policy: ProtectionActivationPolicy | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.client_order_id, str) or not self.client_order_id.strip():
            raise ValueError("protection client_order_id must be non-blank")
        role = self.role if isinstance(self.role, IntentRole) else IntentRole(self.role)
        if role not in {IntentRole.STOP, IntentRole.TARGET}:
            raise ValueError("deferred protection leg must be Stop or Target")
        object.__setattr__(self, "role", role)
        object.__setattr__(
            self,
            "planned_quantity",
            decimal(self.planned_quantity, "planned_quantity", positive=True),
        )
        if self.trigger_price is not None:
            object.__setattr__(
                self,
                "trigger_price",
                decimal(self.trigger_price, "trigger_price", positive=True),
            )
        if self.activation_policy is not None:
            policy = (
                self.activation_policy
                if isinstance(
                    self.activation_policy,
                    ProtectionActivationPolicy,
                )
                else ProtectionActivationPolicy(self.activation_policy)
            )
            object.__setattr__(self, "activation_policy", policy)


@dataclass(frozen=True, slots=True)
class ProtectionActivation:
    client_order_id: str
    role: IntentRole
    desired_total_quantity: Decimal
    increase_quantity: Decimal
    trigger_price: Decimal | None = None


@dataclass(slots=True)
class ProtectionCoordinator:
    entry_client_order_id: str
    entry_requested_quantity: Decimal
    policy: ProtectionActivationPolicy = ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL
    baseline_position_quantity: Decimal = Decimal("0")
    legs: dict[str, DeferredProtectionLeg] = field(default_factory=dict)
    activated_quantities: dict[str, Decimal] = field(default_factory=dict)
    cumulative_entry_fill: Decimal = Decimal("0")
    entry_terminal: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entry_client_order_id, str)
            or not self.entry_client_order_id.strip()
        ):
            raise ValueError("entry client_order_id must be non-blank")
        self.entry_requested_quantity = decimal(
            self.entry_requested_quantity,
            "entry_requested_quantity",
            positive=True,
        )
        self.baseline_position_quantity = decimal(
            self.baseline_position_quantity,
            "baseline_position_quantity",
        )
        if self.baseline_position_quantity < 0:
            raise ValueError("baseline_position_quantity cannot be negative")
        self.policy = (
            self.policy
            if isinstance(self.policy, ProtectionActivationPolicy)
            else ProtectionActivationPolicy(self.policy)
        )
        if not isinstance(self.entry_terminal, bool):
            raise TypeError("entry_terminal must be bool")

    @property
    def total_position_quantity(self) -> Decimal:
        return self.baseline_position_quantity + self.cumulative_entry_fill

    def register_leg(self, leg: DeferredProtectionLeg) -> None:
        existing = self.legs.get(leg.client_order_id)
        if existing is not None:
            if existing != leg:
                raise OrderStateError(
                    PaperReasonCode.INVARIANT_VIOLATION,
                    "deferred protection leg changed",
                )
            return
        maximum = self.baseline_position_quantity + self.entry_requested_quantity
        if leg.planned_quantity > maximum:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "deferred protection exceeds maximum resulting position quantity",
            )
        self.legs[leg.client_order_id] = leg
        self.activated_quantities[leg.client_order_id] = Decimal("0")

    def observe_entry_state(
        self,
        cumulative_quantity: Decimal,
        *,
        entry_terminal: bool,
    ) -> bool:
        cumulative = decimal(cumulative_quantity, "cumulative_entry_fill")
        if not isinstance(entry_terminal, bool):
            raise TypeError("entry_terminal must be bool")
        if cumulative < self.cumulative_entry_fill:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "entry cumulative fill decreased",
            )
        if cumulative > self.entry_requested_quantity:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "entry cumulative fill exceeds requested quantity",
            )
        if self.entry_terminal and not entry_terminal:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "entry terminal state regressed",
            )
        changed = cumulative != self.cumulative_entry_fill or entry_terminal != self.entry_terminal
        self.cumulative_entry_fill = cumulative
        self.entry_terminal = entry_terminal
        return changed

    def pending_activations(self) -> tuple[ProtectionActivation, ...]:
        activations: list[ProtectionActivation] = []
        for client_order_id, leg in self.legs.items():
            desired = self._desired_quantity(leg)
            previous = self.activated_quantities[client_order_id]
            if desired <= previous:
                continue
            activations.append(
                ProtectionActivation(
                    client_order_id=client_order_id,
                    role=leg.role,
                    desired_total_quantity=desired,
                    increase_quantity=desired - previous,
                    trigger_price=leg.trigger_price,
                )
            )
        return tuple(activations)

    def commit_activations(
        self,
        activations: tuple[ProtectionActivation, ...],
    ) -> None:
        expected = self.pending_activations()
        if activations != expected:
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                "protection activation commit is stale or incomplete",
            )
        for activation in activations:
            self.activated_quantities[activation.client_order_id] = (
                activation.desired_total_quantity
            )

    def record_entry_fill(
        self,
        cumulative_quantity: Decimal,
    ) -> tuple[ProtectionActivation, ...]:
        return self.record_entry_state(
            cumulative_quantity,
            entry_terminal=self.entry_terminal,
        )

    def record_entry_state(
        self,
        cumulative_quantity: Decimal,
        *,
        entry_terminal: bool,
    ) -> tuple[ProtectionActivation, ...]:
        self.observe_entry_state(
            cumulative_quantity,
            entry_terminal=entry_terminal,
        )
        activations = self.pending_activations()
        self.commit_activations(activations)
        return activations

    @property
    def protected_quantity(self) -> Decimal:
        stop_quantities = (
            quantity
            for client_order_id, quantity in self.activated_quantities.items()
            if self.legs[client_order_id].role is IntentRole.STOP
        )
        return max(stop_quantities, default=Decimal("0"))

    @property
    def unprotected_quantity(self) -> Decimal:
        return max(
            Decimal("0"),
            self.total_position_quantity - self.protected_quantity,
        )

    def desired_trigger_price(self, role: IntentRole) -> Decimal | None:
        return self._trigger_price(role, committed=False)

    def active_trigger_price(self, role: IntentRole) -> Decimal | None:
        return self._trigger_price(role, committed=True)

    def snapshot_state(self) -> Mapping[str, object]:
        payload = self._state_payload()
        return {**payload, "state_hash": sha256_text(canonical_json(payload))}

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, object],
    ) -> "ProtectionCoordinator":
        base_keys = {
            "schema_version",
            "entry_client_order_id",
            "entry_requested_quantity",
            "policy",
            "legs",
            "activated_quantities",
            "cumulative_entry_fill",
            "state_hash",
        }
        schema_version = state.get("schema_version")
        if schema_version not in {"1", "2", "3", "4", "5"}:
            raise ValueError("unsupported protection state schema")
        versioned_keys: set[str] = set()
        if schema_version in {"3", "4", "5"}:
            versioned_keys.add("entry_terminal")
        if schema_version == "5":
            versioned_keys.add("baseline_position_quantity")
        expected_keys = base_keys | versioned_keys
        if set(state) != expected_keys:
            raise ValueError("invalid protection state fields")
        payload = {key: state[key] for key in expected_keys if key != "state_hash"}
        state_hash = state["state_hash"]
        if not isinstance(state_hash, str) or sha256_text(canonical_json(payload)) != state_hash:
            raise ValueError("protection state hash mismatch")
        raw_legs = state["legs"]
        raw_activated = state["activated_quantities"]
        if not isinstance(raw_legs, Mapping) or not isinstance(
            raw_activated,
            Mapping,
        ):
            raise ValueError("invalid protection leg state")
        if set(raw_legs) != set(raw_activated):
            raise ValueError("protection legs and activated quantities do not match")

        baseline = (
            decimal(
                state["baseline_position_quantity"],
                "baseline_position_quantity",
            )
            if schema_version == "5"
            else Decimal("0")
        )
        if baseline < 0:
            raise ValueError("invalid baseline position quantity")
        coordinator = cls(
            entry_client_order_id=str(state["entry_client_order_id"]),
            entry_requested_quantity=decimal(
                state["entry_requested_quantity"],
                "entry_requested_quantity",
                positive=True,
            ),
            baseline_position_quantity=baseline,
            policy=ProtectionActivationPolicy(str(state["policy"])),
        )
        restored_activated: dict[str, Decimal] = {}
        for client_order_id, raw_leg in raw_legs.items():
            if not isinstance(client_order_id, str) or not isinstance(
                raw_leg,
                Mapping,
            ):
                raise ValueError("invalid protection leg record")
            expected_leg_fields = (
                {"role", "planned_quantity"}
                if schema_version == "1"
                else {
                    "role",
                    "planned_quantity",
                    "trigger_price",
                    "activation_policy",
                }
            )
            if set(raw_leg) != expected_leg_fields:
                raise ValueError("invalid protection leg fields")
            trigger_price = raw_leg.get("trigger_price")
            activation_policy = raw_leg.get("activation_policy")
            coordinator.register_leg(
                DeferredProtectionLeg(
                    client_order_id=client_order_id,
                    role=IntentRole(str(raw_leg["role"])),
                    planned_quantity=decimal(
                        raw_leg["planned_quantity"],
                        "planned_quantity",
                        positive=True,
                    ),
                    trigger_price=(
                        None
                        if trigger_price is None
                        else decimal(
                            trigger_price,
                            "trigger_price",
                            positive=True,
                        )
                    ),
                    activation_policy=(
                        None
                        if activation_policy is None
                        else ProtectionActivationPolicy(str(activation_policy))
                    ),
                )
            )
            activated = decimal(
                raw_activated[client_order_id],
                "activated_quantity",
            )
            if activated < 0 or activated > coordinator.legs[client_order_id].planned_quantity:
                raise ValueError("invalid activated protection quantity")
            restored_activated[client_order_id] = activated

        cumulative = decimal(
            state["cumulative_entry_fill"],
            "cumulative_entry_fill",
        )
        if cumulative < 0 or cumulative > coordinator.entry_requested_quantity:
            raise ValueError("invalid persisted entry fill")
        if schema_version in {"3", "4", "5"}:
            entry_terminal = state["entry_terminal"]
            if not isinstance(entry_terminal, bool):
                raise TypeError("entry_terminal must be bool")
        else:
            entry_terminal = cumulative == coordinator.entry_requested_quantity
        coordinator.observe_entry_state(
            cumulative,
            entry_terminal=entry_terminal,
        )
        for client_order_id, activated in restored_activated.items():
            leg = coordinator.legs[client_order_id]
            if activated > coordinator._desired_quantity(leg):
                raise ValueError("activated protection exceeds eligible factual state")
            coordinator.activated_quantities[client_order_id] = activated
        return coordinator

    def _desired_quantity(self, leg: DeferredProtectionLeg) -> Decimal:
        policy = leg.activation_policy or self.policy
        if (
            policy is ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL
            and self.cumulative_entry_fill == 0
        ):
            return Decimal("0")
        if (
            policy is ProtectionActivationPolicy.AFTER_FULL_FILL
            and self.cumulative_entry_fill < self.entry_requested_quantity
        ):
            return Decimal("0")
        if policy is ProtectionActivationPolicy.AFTER_ENTRY_TERMINAL and not self.entry_terminal:
            return Decimal("0")
        return min(leg.planned_quantity, self.total_position_quantity)

    def _trigger_price(
        self,
        role: IntentRole,
        *,
        committed: bool,
    ) -> Decimal | None:
        normalized_role = role if isinstance(role, IntentRole) else IntentRole(role)
        matches = {
            leg.trigger_price
            for client_order_id, leg in self.legs.items()
            if leg.role is normalized_role
            and (
                self.activated_quantities[client_order_id] > 0
                if committed
                else self._desired_quantity(leg) > 0
            )
            and leg.trigger_price is not None
        }
        if not matches:
            return None
        if len(matches) != 1:
            state_name = "active" if committed else "desired"
            raise OrderStateError(
                PaperReasonCode.INVARIANT_VIOLATION,
                f"multiple {state_name} {normalized_role.value} prices are ambiguous",
            )
        return next(iter(matches))

    def _state_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "5",
            "entry_client_order_id": self.entry_client_order_id,
            "entry_requested_quantity": self.entry_requested_quantity,
            "baseline_position_quantity": self.baseline_position_quantity,
            "policy": self.policy.value,
            "legs": {
                client_order_id: {
                    "role": leg.role.value,
                    "planned_quantity": leg.planned_quantity,
                    "trigger_price": leg.trigger_price,
                    "activation_policy": (
                        leg.activation_policy.value if leg.activation_policy is not None else None
                    ),
                }
                for client_order_id, leg in sorted(self.legs.items())
            },
            "activated_quantities": dict(sorted(self.activated_quantities.items())),
            "cumulative_entry_fill": self.cumulative_entry_fill,
            "entry_terminal": self.entry_terminal,
        }
