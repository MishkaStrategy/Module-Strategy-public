from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ._utils import canonical_json, sha256_text
from .oco import OcoGroup
from .protection import ProtectionCoordinator


@dataclass(slots=True)
class ExecutionOrchestrationState:
    oco_groups: dict[str, OcoGroup] = field(default_factory=dict)
    protection_by_entry: dict[str, ProtectionCoordinator] = field(default_factory=dict)

    def register_oco(self, group: OcoGroup) -> None:
        existing = self.oco_groups.get(group.group_id)
        if existing is not None:
            if existing.snapshot_state() != group.snapshot_state():
                raise ValueError("OCO group state changed during duplicate registration")
            return
        self.oco_groups[group.group_id] = group

    def register_protection(self, coordinator: ProtectionCoordinator) -> None:
        key = coordinator.entry_client_order_id
        existing = self.protection_by_entry.get(key)
        if existing is not None:
            if existing.snapshot_state() != coordinator.snapshot_state():
                raise ValueError("protection state changed during duplicate registration")
            return
        self.protection_by_entry[key] = coordinator

    def snapshot_state(self) -> Mapping[str, object]:
        payload = self._state_payload()
        return {**payload, "state_hash": sha256_text(canonical_json(payload))}

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> "ExecutionOrchestrationState":
        expected_keys = {
            "schema_version",
            "oco_groups",
            "protection_by_entry",
            "state_hash",
        }
        if set(state) != expected_keys:
            raise ValueError("invalid orchestration state fields")
        if state["schema_version"] != "1":
            raise ValueError("unsupported orchestration state schema")
        state_hash = state["state_hash"]
        payload = {key: state[key] for key in expected_keys if key != "state_hash"}
        if not isinstance(state_hash, str) or sha256_text(canonical_json(payload)) != state_hash:
            raise ValueError("orchestration state hash mismatch")
        raw_oco = state["oco_groups"]
        raw_protection = state["protection_by_entry"]
        if not isinstance(raw_oco, Mapping) or not isinstance(raw_protection, Mapping):
            raise ValueError("invalid orchestration aggregate state")

        restored = cls()
        for group_id, raw_group in raw_oco.items():
            if not isinstance(group_id, str) or not isinstance(raw_group, Mapping):
                raise ValueError("invalid OCO aggregate record")
            group = OcoGroup.from_state(raw_group)
            if group.group_id != group_id:
                raise ValueError("OCO aggregate key mismatch")
            restored.register_oco(group)
        for entry_id, raw_coordinator in raw_protection.items():
            if not isinstance(entry_id, str) or not isinstance(raw_coordinator, Mapping):
                raise ValueError("invalid protection aggregate record")
            coordinator = ProtectionCoordinator.from_state(raw_coordinator)
            if coordinator.entry_client_order_id != entry_id:
                raise ValueError("protection aggregate key mismatch")
            restored.register_protection(coordinator)
        return restored

    def _state_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "oco_groups": {
                group_id: group.snapshot_state()
                for group_id, group in sorted(self.oco_groups.items())
            },
            "protection_by_entry": {
                entry_id: coordinator.snapshot_state()
                for entry_id, coordinator in sorted(self.protection_by_entry.items())
            },
        }
