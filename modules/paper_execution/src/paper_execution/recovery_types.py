from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import ExecutionResult
from .orchestration_state import ExecutionOrchestrationState
from .state_machine import OrderAggregate

STATE_SCHEMA_VERSION = "4"
LEGACY_STATE_SCHEMA_VERSIONS = frozenset({"1", "2", "3"})
SUPPORTED_STATE_SCHEMA_VERSIONS = frozenset({STATE_SCHEMA_VERSION, *LEGACY_STATE_SCHEMA_VERSIONS})
ORCHESTRATION_STATE_SCHEMAS = frozenset({"2", "3", "4"})
EVENT_FINGERPRINT_SCHEMAS = frozenset({"3", "4"})
EVENT_TIMESTAMP_SCHEMAS = frozenset({"4"})
MAX_STATE_RECORDS = 1_000_000


@dataclass(frozen=True, slots=True)
class RestoredExecutionState:
    plan_results: dict[str, ExecutionResult]
    plan_hashes: dict[str, str]
    plan_scopes: dict[str, tuple[str, str]]
    orders: dict[str, OrderAggregate]
    blocked_scopes: set[tuple[str, str]]
    event_sequence: int
    journal_attestation: Mapping[str, object]
    orchestration_state: ExecutionOrchestrationState
    source_schema_version: str


def require_keys(
    row: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    if set(row) != expected:
        raise ValueError(f"{name} contains missing or unknown fields")


def mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


def sequence(value: object, name: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(
        value,
        (list, tuple),
    ):
        raise TypeError(f"{name} must be a sequence")
    return tuple(value)


def string(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise TypeError(f"{name} must be a string")
    return value


def hash_digest(value: object, name: str) -> str:
    text = string(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int")
    return value
