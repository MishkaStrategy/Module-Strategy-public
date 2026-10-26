from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import re
from typing import Any, cast

from .extensions import StateRepository
from .manifest import manifest_for
from .serialization import deep_freeze, to_jsonable
from .validation import require_nonblank, utc_datetime


@dataclass(frozen=True, slots=True)
class ModuleState:
    module: str
    module_version: str
    schema_version: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_nonblank(self.module, "module state module")
        require_nonblank(self.module_version, "module state version")
        require_nonblank(self.schema_version, "module state schema_version")
        if not isinstance(self.payload, Mapping):
            raise TypeError("module state payload must be a mapping")
        object.__setattr__(self, "payload", deep_freeze(self.payload))


@dataclass(frozen=True, slots=True)
class KernelCheckpoint:
    checkpoint_id: str
    run_id: str
    kernel_version: str
    created_at: datetime
    last_bar_index: int
    last_bar_time: datetime
    data_hash: str
    config_hash: str
    module_hash: str
    module_states: tuple[ModuleState, ...]
    schema_version: str = "1"
    integrity_sha256: str | None = None

    def __post_init__(self) -> None:
        require_nonblank(self.checkpoint_id, "checkpoint_id")
        require_nonblank(self.run_id, "checkpoint run_id")
        require_nonblank(self.kernel_version, "checkpoint kernel_version")
        object.__setattr__(
            self,
            "created_at",
            utc_datetime(self.created_at, "checkpoint created_at"),
        )
        object.__setattr__(
            self,
            "last_bar_time",
            utc_datetime(self.last_bar_time, "checkpoint last_bar_time"),
        )
        if self.created_at < self.last_bar_time:
            raise ValueError("checkpoint created_at cannot precede last_bar_time")
        if not isinstance(self.last_bar_index, int) or isinstance(
            self.last_bar_index,
            bool,
        ):
            raise TypeError("checkpoint last_bar_index must be int")
        if self.last_bar_index < 0:
            raise ValueError("checkpoint last_bar_index cannot be negative")
        require_nonblank(self.data_hash, "checkpoint data_hash")
        require_nonblank(self.config_hash, "checkpoint config_hash")
        require_nonblank(self.module_hash, "checkpoint module_hash")
        require_nonblank(self.schema_version, "checkpoint schema_version")
        states = _module_state_tuple(self.module_states)
        object.__setattr__(self, "module_states", states)
        names = [state.module for state in states]
        if len(names) != len(set(names)):
            raise ValueError("checkpoint contains duplicate module states")
        if self.integrity_sha256 is not None:
            if not isinstance(self.integrity_sha256, str):
                raise TypeError("checkpoint integrity_sha256 must be str or None")
            if re.fullmatch(r"[0-9a-f]{64}", self.integrity_sha256) is None:
                raise ValueError(
                    "checkpoint integrity_sha256 must be a canonical lowercase SHA-256 hex digest"
                )


def capture_module_states(modules: Sequence[Any]) -> tuple[ModuleState, ...]:
    module_list = _module_tuple(modules)
    states: list[ModuleState] = []
    for module in module_list:
        manifest = manifest_for(module)
        payload = module.snapshot_state()
        if not isinstance(payload, Mapping):
            raise TypeError(f"{module.name}.snapshot_state() must return a mapping")
        states.append(
            ModuleState(
                module=module.name,
                module_version=manifest.version,
                schema_version=manifest.state_schema_version,
                payload=payload,
            )
        )
    return tuple(states)


def restore_module_states(
    modules: Sequence[Any],
    states: Sequence[ModuleState],
    *,
    strict: bool = True,
) -> None:
    if not isinstance(strict, bool):
        raise TypeError("strict must be bool")
    module_list = _module_tuple(modules)
    state_list = _module_state_tuple(states)
    by_name = {state.module: state for state in state_list}
    if len(by_name) != len(state_list):
        raise ValueError("checkpoint contains duplicate module states")
    module_names = {module.name for module in module_list}
    if strict:
        missing = sorted(module_names - set(by_name))
        extra = sorted(set(by_name) - module_names)
        if missing or extra:
            raise ValueError(f"checkpoint module mismatch; missing={missing}, extra={extra}")

    plan: list[tuple[Any, ModuleState]] = []
    originals: dict[str, Mapping[str, Any]] = {}
    for module in module_list:
        state = by_name.get(module.name)
        if state is None:
            continue
        manifest = manifest_for(module)
        if state.module_version != manifest.version:
            raise ValueError(
                f"module version mismatch for {module.name}: "
                f"checkpoint={state.module_version}, current={manifest.version}"
            )
        if state.schema_version != manifest.state_schema_version:
            raise ValueError(
                f"state schema mismatch for {module.name}: "
                f"checkpoint={state.schema_version}, current={manifest.state_schema_version}"
            )
        original = module.snapshot_state()
        if not isinstance(original, Mapping):
            raise TypeError(f"{module.name}.snapshot_state() must return a mapping")
        frozen_original = deep_freeze(original)
        if not isinstance(frozen_original, Mapping):
            raise RuntimeError("frozen module state must remain a mapping")
        originals[module.name] = cast(Mapping[str, Any], frozen_original)
        plan.append((module, state))

    attempted: list[Any] = []
    try:
        for module, state in plan:
            attempted.append(module)
            module.restore_state(state.payload)
    except Exception as restore_error:
        rollback_errors: list[tuple[str, Exception]] = []
        for module in reversed(attempted):
            try:
                module.restore_state(originals[module.name])
            except Exception as rollback_error:
                rollback_errors.append((module.name, rollback_error))
        if rollback_errors:
            details = ", ".join(
                f"{name}: {type(error).__name__}: {error}" for name, error in rollback_errors
            )
            raise RuntimeError(
                f"module state restore failed and rollback was incomplete ({details})"
            ) from restore_error
        raise


def build_checkpoint(
    *,
    run_id: str,
    kernel_version: str,
    created_at: datetime,
    last_bar_index: int,
    last_bar_time: datetime,
    data_hash: str,
    config_hash: str,
    module_hash: str,
    modules: Sequence[Any],
) -> KernelCheckpoint:
    validated = KernelCheckpoint(
        checkpoint_id="checkpoint-pending",
        run_id=run_id,
        kernel_version=kernel_version,
        created_at=created_at,
        last_bar_index=last_bar_index,
        last_bar_time=last_bar_time,
        data_hash=data_hash,
        config_hash=config_hash,
        module_hash=module_hash,
        module_states=(),
    )
    states = capture_module_states(modules)
    identity_payload = {
        "run_id": validated.run_id,
        "kernel_version": validated.kernel_version,
        "last_bar_index": validated.last_bar_index,
        "last_bar_time": validated.last_bar_time.isoformat(),
        "data_hash": validated.data_hash,
        "config_hash": validated.config_hash,
        "module_hash": validated.module_hash,
        "schema_version": validated.schema_version,
        "states": to_jsonable(states),
    }
    digest = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    checkpoint = KernelCheckpoint(
        checkpoint_id=f"checkpoint-{digest}",
        run_id=validated.run_id,
        kernel_version=validated.kernel_version,
        created_at=validated.created_at,
        last_bar_index=validated.last_bar_index,
        last_bar_time=validated.last_bar_time,
        data_hash=validated.data_hash,
        config_hash=validated.config_hash,
        module_hash=validated.module_hash,
        module_states=states,
        schema_version=validated.schema_version,
    )
    return KernelCheckpoint(
        **{
            **to_jsonable(checkpoint),
            "created_at": checkpoint.created_at,
            "last_bar_time": checkpoint.last_bar_time,
            "module_states": checkpoint.module_states,
            "integrity_sha256": checkpoint_digest(checkpoint),
        }
    )


def checkpoint_digest(checkpoint: KernelCheckpoint) -> str:
    if not isinstance(checkpoint, KernelCheckpoint):
        raise TypeError("checkpoint must be KernelCheckpoint")
    payload = to_jsonable(checkpoint)
    payload.pop("integrity_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_checkpoint_integrity(checkpoint: KernelCheckpoint) -> None:
    if checkpoint.integrity_sha256 is None:
        return
    expected = checkpoint_digest(checkpoint)
    if not hmac.compare_digest(checkpoint.integrity_sha256, expected):
        raise ValueError("checkpoint integrity verification failed")


class CheckpointManager:
    def __init__(
        self,
        repository: StateRepository,
        *,
        supported_schema_versions: Sequence[str] = ("1",),
    ) -> None:
        if isinstance(supported_schema_versions, (str, bytes)) or not isinstance(
            supported_schema_versions,
            Sequence,
        ):
            raise TypeError("supported_schema_versions must be a sequence")
        versions = tuple(supported_schema_versions)
        if not versions or any(
            not isinstance(version, str) or not version.strip() or version != version.strip()
            for version in versions
        ):
            raise ValueError("supported_schema_versions must contain canonical nonblank strings")
        if len(versions) != len(set(versions)):
            raise ValueError("supported_schema_versions cannot contain duplicates")
        self.repository = repository
        self.supported_schema_versions = frozenset(versions)

    def save(self, key: str, checkpoint: KernelCheckpoint) -> None:
        if not isinstance(checkpoint, KernelCheckpoint):
            raise TypeError("checkpoint must be KernelCheckpoint")
        if checkpoint.schema_version not in self.supported_schema_versions:
            raise ValueError(f"unsupported checkpoint schema: {checkpoint.schema_version}")
        if checkpoint.integrity_sha256 is None:
            raise ValueError("checkpoint integrity_sha256 is required for persistence")
        _verify_checkpoint_integrity(checkpoint)
        self.repository.save(key, to_jsonable(checkpoint))

    def load(self, key: str) -> KernelCheckpoint | None:
        raw = self.repository.load(key)
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise TypeError("checkpoint payload must be a mapping")
        schema_version = raw.get("schema_version", "1")
        if not isinstance(schema_version, str):
            raise TypeError("checkpoint schema_version must be str")
        if not schema_version.strip() or schema_version != schema_version.strip():
            raise ValueError("checkpoint schema_version must be canonical and nonblank")
        if schema_version not in self.supported_schema_versions:
            raise ValueError(f"unsupported checkpoint schema: {schema_version}")
        raw_states = raw.get("module_states", ())
        if isinstance(raw_states, (str, bytes)) or not isinstance(
            raw_states,
            Sequence,
        ):
            raise TypeError("checkpoint module_states must be a sequence")
        states: list[ModuleState] = []
        for index, row in enumerate(raw_states):
            if not isinstance(row, Mapping):
                raise TypeError(f"checkpoint module_states[{index}] must be a mapping")
            context = f"checkpoint module_states[{index}]"
            states.append(
                ModuleState(
                    module=_required_field(row, "module", context=context),
                    module_version=_required_field(
                        row,
                        "module_version",
                        context=context,
                    ),
                    schema_version=_required_field(
                        row,
                        "schema_version",
                        context=context,
                    ),
                    payload=row.get("payload", {}),
                )
            )
        checkpoint = KernelCheckpoint(
            checkpoint_id=_required_field(raw, "checkpoint_id"),
            run_id=_required_field(raw, "run_id"),
            kernel_version=_required_field(raw, "kernel_version"),
            created_at=_parse_datetime(
                _required_field(raw, "created_at"),
                "checkpoint created_at",
            ),
            last_bar_index=_required_field(raw, "last_bar_index"),
            last_bar_time=_parse_datetime(
                _required_field(raw, "last_bar_time"),
                "checkpoint last_bar_time",
            ),
            data_hash=_required_field(raw, "data_hash"),
            config_hash=_required_field(raw, "config_hash"),
            module_hash=_required_field(raw, "module_hash"),
            module_states=tuple(states),
            schema_version=schema_version,
            integrity_sha256=raw.get("integrity_sha256"),
        )
        _verify_checkpoint_integrity(checkpoint)
        return checkpoint


def _required_field(
    payload: Mapping[str, Any],
    field: str,
    *,
    context: str = "checkpoint payload",
) -> Any:
    if field not in payload:
        raise ValueError(f"{context} missing required field: {field}")
    return payload[field]


def _module_tuple(modules: Sequence[Any]) -> tuple[Any, ...]:
    if isinstance(modules, (str, bytes)) or not isinstance(modules, Sequence):
        raise TypeError("modules must be a sequence")
    result = tuple(modules)
    names: list[str] = []
    for module in result:
        name = getattr(module, "name", None)
        if not isinstance(name, str):
            raise TypeError("module name must be str")
        require_nonblank(name, "module name")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("module names must be unique")
    return result


def _module_state_tuple(states: Sequence[ModuleState]) -> tuple[ModuleState, ...]:
    if isinstance(states, (str, bytes)) or not isinstance(states, Sequence):
        raise TypeError("module_states must be a sequence")
    result: list[ModuleState] = []
    for state in states:
        if not isinstance(state, ModuleState):
            raise TypeError("module_states must contain ModuleState")
        result.append(state)
    return tuple(result)


def _parse_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO datetime string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid ISO datetime") from exc
