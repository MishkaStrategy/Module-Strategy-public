from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import require_json_mapping
from .errors import CheckpointIntegrityError


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > 128:
        raise ValueError(f"{name} exceeds maximum length")
    return value


class RuntimeMetadataComponent(Protocol):
    def snapshot_runtime_metadata(self) -> Mapping[str, Any]: ...

    def restore_runtime_metadata(self, state: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class NamedRuntimeMetadata:
    name: str
    component: RuntimeMetadataComponent

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "metadata namespace"))
        if not callable(getattr(self.component, "snapshot_runtime_metadata", None)):
            raise TypeError("metadata component must implement snapshot_runtime_metadata()")
        if not callable(getattr(self.component, "restore_runtime_metadata", None)):
            raise TypeError("metadata component must implement restore_runtime_metadata()")


class CompositeRuntimeMetadata:
    """Namespaced, transactionally restored runtime checkpoint metadata."""

    def __init__(self, components: Sequence[NamedRuntimeMetadata]) -> None:
        if isinstance(components, (str, bytes)) or not isinstance(components, Sequence):
            raise TypeError("components must be a sequence")
        normalized = tuple(components)
        if not normalized or any(not isinstance(item, NamedRuntimeMetadata) for item in normalized):
            raise ValueError("components must contain NamedRuntimeMetadata values")
        names = tuple(item.name for item in normalized)
        if len(names) != len(set(names)):
            raise ValueError("metadata namespaces cannot contain duplicates")
        self._components = tuple(sorted(normalized, key=lambda item: item.name))

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._components)

    def snapshot_runtime_metadata(self) -> Mapping[str, Any]:
        return {
            item.name: require_json_mapping(
                item.component.snapshot_runtime_metadata(),
                f"runtime metadata {item.name}",
            )
            for item in self._components
        }

    def restore_runtime_metadata(self, state: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("composite runtime metadata must be a mapping")
        expected = set(self.namespaces)
        actual = set(state)
        if actual != expected:
            raise CheckpointIntegrityError(
                "runtime metadata namespace mismatch; "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        decoded: dict[str, Mapping[str, Any]] = {}
        originals: dict[str, Mapping[str, Any]] = {}
        for item in self._components:
            decoded[item.name] = require_json_mapping(
                state[item.name],
                f"runtime metadata {item.name}",
            )
            originals[item.name] = require_json_mapping(
                item.component.snapshot_runtime_metadata(),
                f"current runtime metadata {item.name}",
            )
        attempted: list[NamedRuntimeMetadata] = []
        try:
            for item in self._components:
                attempted.append(item)
                item.component.restore_runtime_metadata(decoded[item.name])
        except Exception as exc:
            rollback_errors: list[tuple[str, Exception]] = []
            for item in reversed(attempted):
                try:
                    item.component.restore_runtime_metadata(originals[item.name])
                except Exception as rollback_error:
                    rollback_errors.append((item.name, rollback_error))
            if rollback_errors:
                details = ", ".join(
                    f"{name}:{type(error).__name__}:{error}" for name, error in rollback_errors
                )
                raise CheckpointIntegrityError(
                    "runtime metadata restore failed and rollback was incomplete: " + details
                ) from exc
            raise CheckpointIntegrityError("runtime metadata restore failed") from exc
