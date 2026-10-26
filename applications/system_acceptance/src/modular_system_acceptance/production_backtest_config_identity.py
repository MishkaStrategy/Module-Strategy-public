from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .contracts import AcceptanceErrorCode, AcceptanceOwnerError
from .production_identity import require_identifier, require_sha256

_REGISTERED_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_REGISTERED_BATCH_SIZE_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"
_REGISTERED_MODES = frozenset({"legacy_materialized", "streaming"})
_MAX_MODULE_CONFIG_BYTES = 1024 * 1024
_BACKTEST_CONFIG_FIELDS = frozenset(
    {
        "data",
        "modules",
        "initial_capital",
        "requested_timeframes",
        "execution",
        "fees",
        "risk",
        "instrument",
        "output",
        "seed",
        "checkpoint",
        "end_of_test_policy",
    }
)
_DATA_SOURCE_FIELDS = frozenset(
    {
        "symbol",
        "path",
        "format",
        "market_type",
        "base_timeframe",
        "gap_policy",
        "timestamp_column",
        "start",
        "end",
        "columns",
        "storage_root",
        "dataset_id",
        "dataset_version",
        "registered_mode",
        "stream_batch_size",
    }
)
_CHECKPOINT_FIELDS = frozenset({"mode", "restart_at_bar"})
_OUTPUT_FIELDS = frozenset({"directory"})
_PERFORMANCE_FIELDS = frozenset(
    {
        "mode",
        "max_memory_mb",
        "max_events",
        "max_skip_ranges",
        "max_diagnostics",
        "max_equity_points",
        "progress_interval_bars",
        "retained_event_window",
        "equity_sampling",
        "equity_every_n_bars",
        "max_base_history",
        "max_timeframe_history",
        "disable_cyclic_gc_in_hot_loop",
    }
)


@dataclass(frozen=True, slots=True)
class BacktestExecutionIdentity:
    """Safe identity of the complete frozen Backtest execution configuration."""

    config_digest: str
    performance_digest: str
    valuation_policy: str
    funding_identity_digest: str | None
    mark_identity_digest: str | None

    def __post_init__(self) -> None:
        require_sha256(self.config_digest, "Backtest configuration digest")
        require_sha256(self.performance_digest, "Backtest performance digest")
        if self.valuation_policy not in {"last", "mark"}:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "Backtest valuation policy is unsupported",
            )
        if self.funding_identity_digest is not None:
            require_sha256(self.funding_identity_digest, "Backtest funding identity digest")
        if self.mark_identity_digest is not None:
            require_sha256(self.mark_identity_digest, "Backtest mark identity digest")
        if self.valuation_policy == "mark" and self.mark_identity_digest is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "mark valuation requires a registered mark identity",
            )
        if self.valuation_policy == "last" and self.mark_identity_digest is not None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "last valuation must not include a mark identity",
            )


def build_backtest_execution_identity(
    *,
    backtest_a_config: object,
    backtest_b_config: object,
    recovery_config: object,
    performance: object,
    dataset_id: str,
    dataset_digest: str,
    dataset_version: int,
    dataset_timeframe: str,
    valuation_policy: str,
    funding_source: object | None = None,
    mark_source: object | None = None,
    environment: Mapping[str, str] | None = None,
) -> BacktestExecutionIdentity:
    """Canonicalize real Backtest objects without importing the Backtest package.

    Deployment passes authoritative ``BacktestAppConfig`` / ``PerformanceConfig``
    objects. Exact dataclass field sets are validated so Backtest schema growth fails
    closed until this identity contract is deliberately revised.
    """

    require_identifier(dataset_id, "Backtest dataset_id")
    require_sha256(dataset_digest, "Backtest dataset digest")
    if (
        isinstance(dataset_version, bool)
        or not isinstance(dataset_version, int)
        or dataset_version <= 0
    ):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "Backtest dataset version must be a positive integer",
        )
    require_identifier(dataset_timeframe, "Backtest dataset timeframe")
    normalized_policy = _identifier_value(valuation_policy, "Backtest valuation policy")
    if normalized_policy not in {"last", "mark"}:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "Backtest valuation policy is unsupported",
        )

    env = dict(os.environ if environment is None else environment)
    backtest_a = _backtest_config_mapping(
        backtest_a_config,
        dataset_id=dataset_id,
        dataset_digest=dataset_digest,
        dataset_version=dataset_version,
        dataset_timeframe=dataset_timeframe,
        environment=env,
        role="backtest_a",
    )
    backtest_b = _backtest_config_mapping(
        backtest_b_config,
        dataset_id=dataset_id,
        dataset_digest=dataset_digest,
        dataset_version=dataset_version,
        dataset_timeframe=dataset_timeframe,
        environment=env,
        role="backtest_b",
    )
    recovery = _backtest_config_mapping(
        recovery_config,
        dataset_id=dataset_id,
        dataset_digest=dataset_digest,
        dataset_version=dataset_version,
        dataset_timeframe=dataset_timeframe,
        environment=env,
        role="recovery",
    )
    _require_backtest_role_contract(backtest_a, backtest_b, recovery)

    performance_mapping = _performance_mapping(performance)
    config_digest = _sha256_json(
        {
            "schema_version": 1,
            "backtest_a": backtest_a,
            "backtest_b": backtest_b,
            "recovery": recovery,
        }
    )
    performance_digest = _sha256_json(
        {
            "schema_version": 1,
            "performance": performance_mapping,
        }
    )
    funding_digest = _optional_owner_identity(funding_source, "funding")
    mark_digest = _optional_owner_identity(mark_source, "mark")
    return BacktestExecutionIdentity(
        config_digest=config_digest,
        performance_digest=performance_digest,
        valuation_policy=normalized_policy,
        funding_identity_digest=funding_digest,
        mark_identity_digest=mark_digest,
    )


def _backtest_config_mapping(
    config: object,
    *,
    dataset_id: str,
    dataset_digest: str,
    dataset_version: int,
    dataset_timeframe: str,
    environment: Mapping[str, str],
    role: str,
) -> dict[str, object]:
    _require_dataclass_fields(config, _BACKTEST_CONFIG_FIELDS, f"{role} Backtest config")
    data = _attribute(config, "data", role)
    checkpoint = _attribute(config, "checkpoint", role)
    output = _attribute(config, "output", role)
    _require_dataclass_fields(data, _DATA_SOURCE_FIELDS, f"{role} Backtest data")
    _require_dataclass_fields(checkpoint, _CHECKPOINT_FIELDS, f"{role} Backtest checkpoint")
    _require_dataclass_fields(output, _OUTPUT_FIELDS, f"{role} Backtest output")

    observed_dataset_id = _attribute(data, "dataset_id", f"{role}.data")
    observed_dataset_version = _attribute(data, "dataset_version", f"{role}.data")
    observed_timeframe = _attribute(data, "base_timeframe", f"{role}.data")
    if (
        observed_dataset_id != dataset_id
        or observed_dataset_version != dataset_version
        or observed_timeframe != dataset_timeframe
    ):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest registered dataset identity differs from deployment identity",
        )
    if (
        _attribute(data, "path", f"{role}.data") is not None
        or _attribute(
            data,
            "format",
            f"{role}.data",
        )
        is not None
    ):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest must use the registered dataset owner",
        )
    if _attribute(data, "storage_root", f"{role}.data") is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest registered dataset storage is not configured",
        )

    effective_mode, effective_batch = _effective_registered_streaming(data, environment)
    modules_digest = _module_configuration_digest(_attribute(config, "modules", role), role)
    return {
        "data": {
            "dataset_id": dataset_id,
            "dataset_digest": dataset_digest,
            "dataset_version": dataset_version,
            "symbol": _string_value(
                _attribute(data, "symbol", f"{role}.data"),
                "data.symbol",
            ),
            "market_type": _string_value(
                _attribute(data, "market_type", f"{role}.data"),
                "data.market_type",
            ),
            "base_timeframe": dataset_timeframe,
            "gap_policy": _string_value(
                _attribute(data, "gap_policy", f"{role}.data"),
                "data.gap_policy",
            ),
            "timestamp_column": _string_value(
                _attribute(data, "timestamp_column", f"{role}.data"),
                "data.timestamp_column",
            ),
            "columns": _json_mapping(
                _attribute(data, "columns", f"{role}.data"),
                "data.columns",
            ),
            "start": _optional_datetime(
                _attribute(data, "start", f"{role}.data"),
                "data.start",
            ),
            "end": _optional_datetime(
                _attribute(data, "end", f"{role}.data"),
                "data.end",
            ),
            "registered_mode": effective_mode,
            "stream_batch_size": effective_batch,
        },
        "modules_config_digest": modules_digest,
        "initial_capital": _json_scalar(
            _attribute(config, "initial_capital", role),
            "initial_capital",
        ),
        "requested_timeframes": _string_sequence(
            _attribute(config, "requested_timeframes", role),
            "requested_timeframes",
        ),
        "execution": _json_mapping(_attribute(config, "execution", role), "execution"),
        "fees": _json_mapping(_attribute(config, "fees", role), "fees"),
        "risk": _json_mapping(_attribute(config, "risk", role), "risk"),
        "instrument": _json_mapping(_attribute(config, "instrument", role), "instrument"),
        "seed": _integer_value(_attribute(config, "seed", role), "seed"),
        "checkpoint": {
            "mode": _string_value(
                _attribute(checkpoint, "mode", f"{role}.checkpoint"),
                "checkpoint.mode",
            ),
            "restart_at_bar": _optional_nonnegative_integer(
                _attribute(checkpoint, "restart_at_bar", f"{role}.checkpoint"),
                "checkpoint.restart_at_bar",
            ),
        },
        "end_of_test_policy": _string_value(
            _attribute(config, "end_of_test_policy", role),
            "end_of_test_policy",
        ),
    }


def _require_backtest_role_contract(
    backtest_a: Mapping[str, object],
    backtest_b: Mapping[str, object],
    recovery: Mapping[str, object],
) -> None:
    if backtest_a != backtest_b:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest A and B execution semantics must be identical",
        )
    a_checkpoint = _checkpoint_mapping(backtest_a, "Backtest A")
    b_checkpoint = _checkpoint_mapping(backtest_b, "Backtest B")
    recovery_checkpoint = _checkpoint_mapping(recovery, "Backtest recovery")
    if a_checkpoint != {"mode": "disabled", "restart_at_bar": None} or b_checkpoint != {
        "mode": "disabled",
        "restart_at_bar": None,
    }:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest A and B must use checkpoint mode disabled",
        )
    if recovery_checkpoint.get("mode") != "replay":
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest recovery must use checkpoint mode replay",
        )
    a_without_checkpoint = {key: value for key, value in backtest_a.items() if key != "checkpoint"}
    recovery_without_checkpoint = {
        key: value for key, value in recovery.items() if key != "checkpoint"
    }
    if a_without_checkpoint != recovery_without_checkpoint:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            "Backtest recovery may differ from A/B only by checkpoint semantics",
        )


def _checkpoint_mapping(value: Mapping[str, object], label: str) -> dict[str, object]:
    checkpoint = value.get("checkpoint")
    if not isinstance(checkpoint, dict):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{label} checkpoint identity is invalid",
        )
    return cast(dict[str, object], checkpoint)


def _performance_mapping(performance: object) -> dict[str, object]:
    _require_dataclass_fields(performance, _PERFORMANCE_FIELDS, "Backtest performance config")
    return {
        name: _json_scalar(_attribute(performance, name, "performance"), name)
        for name in sorted(_PERFORMANCE_FIELDS)
    }


def _effective_registered_streaming(
    data: object,
    environment: Mapping[str, str],
) -> tuple[str, int | None]:
    configured_mode = _string_value(
        _attribute(data, "registered_mode", "data"),
        "registered_mode",
    )
    raw_mode = environment.get(_REGISTERED_MODE_ENV, configured_mode)
    if not isinstance(raw_mode, str):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "registered dataset mode override must be a string",
        )
    mode = raw_mode.strip().lower()
    if mode not in _REGISTERED_MODES:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "registered dataset mode override is unsupported",
        )
    configured_batch = _attribute(data, "stream_batch_size", "data")
    raw_batch = environment.get(_REGISTERED_BATCH_SIZE_ENV)
    if raw_batch is not None:
        try:
            batch = int(raw_batch)
        except ValueError as exc:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "registered stream batch override must be a positive integer",
            ) from exc
        if batch <= 0:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "registered stream batch override must be a positive integer",
            )
    elif configured_batch is None:
        batch = None
    else:
        batch = _positive_integer(configured_batch, "stream_batch_size")
    if mode == "legacy_materialized":
        if raw_batch is not None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "registered stream batch override requires streaming mode",
            )
        return mode, None
    if batch is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "streaming Backtest configuration requires a resolved batch size",
        )
    return mode, batch


def _module_configuration_digest(value: object, role: str) -> str:
    if not isinstance(value, Path):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{role} Backtest modules configuration must be a Path",
        )
    if value.is_symlink():
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest modules configuration must not be a symlink",
        )
    try:
        data = value.read_bytes()
    except OSError as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest modules configuration is unavailable",
        ) from exc
    if not data or len(data) > _MAX_MODULE_CONFIG_BYTES:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{role} Backtest modules configuration has an invalid size",
        )
    return hashlib.sha256(data).hexdigest()


def _optional_owner_identity(value: object | None, label: str) -> str | None:
    if value is None:
        return None
    callback = getattr(value, "identity_sha256", None)
    if not callable(callback):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"Backtest {label} source must expose identity_sha256()",
        )
    try:
        digest = callback()
    except Exception as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"Backtest {label} source identity is unavailable",
        ) from exc
    return require_sha256(digest, f"Backtest {label} source identity")


def _require_dataclass_fields(
    value: object,
    expected: frozenset[str],
    label: str,
) -> None:
    if not is_dataclass(value) or isinstance(value, type):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{label} must be a dataclass instance",
        )
    observed = frozenset(field.name for field in fields(cast(Any, value)))
    if observed != expected:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.PHASE_BLOCKED,
            f"{label} schema differs from the accepted identity contract",
        )


def _attribute(value: object, name: str, label: str) -> Any:
    if not hasattr(value, name):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{label} is missing required field {name}",
        )
    return getattr(value, name)


def _sha256_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            "Backtest configuration contains a noncanonical value",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _json_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be a string-keyed mapping",
        )
    return {key: _json_value(item, f"{name}.{key}") for key, item in sorted(value.items())}


def _json_value(value: object, name: str) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return _json_scalar(value, name)
    if isinstance(value, Mapping):
        return _json_mapping(value, name)
    if isinstance(value, tuple | list):
        return tuple(_json_value(item, name) for item in value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str | int):
        return enum_value
    raise AcceptanceOwnerError(
        AcceptanceErrorCode.INVALID_REQUEST,
        f"{name} contains a noncanonical value",
    )


def _json_scalar(value: object, name: str) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError as exc:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                f"{name} must be finite",
            ) from exc
        return value
    raise AcceptanceOwnerError(
        AcceptanceErrorCode.INVALID_REQUEST,
        f"{name} must be a canonical scalar",
    )


def _string_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple | list) or isinstance(value, str | bytes):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be a sequence",
        )
    result = tuple(_string_value(item, name) for item in value)
    if not result or len(result) != len(set(result)):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be non-empty and unique",
        )
    return result


def _string_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be a canonical non-empty string",
        )
    return value


def _identifier_value(value: object, name: str) -> str:
    rendered = _string_value(value, name)
    return require_identifier(rendered, name)


def _integer_value(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be an integer",
        )
    return value


def _positive_integer(value: object, name: str) -> int:
    result = _integer_value(value, name)
    if result <= 0:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be a positive integer",
        )
    return result


def _optional_nonnegative_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    result = _integer_value(value, name)
    if result < 0:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be non-negative",
        )
    return result


def _optional_datetime(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.INVALID_REQUEST,
            f"{name} must be timezone-aware",
        )
    return value.isoformat()


__all__ = [
    "BacktestExecutionIdentity",
    "build_backtest_execution_identity",
]
