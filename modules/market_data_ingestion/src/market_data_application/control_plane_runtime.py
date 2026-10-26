from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from .control_plane_contracts import TokenReloadRequest
from .control_plane_http import ControlPlaneSecurityController
from .control_plane_security import (
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
    utc_now,
)
from .control_plane_store import AuditContext, ControlPlaneStateError, SQLiteControlPlaneStore
from .settings import ApplicationSettings


def control_plane_from_environment(
    settings: ApplicationSettings,
    environment: Mapping[str, str] | None = None,
) -> ControlPlaneSecurityController | None:
    values = environment or {}
    validate_control_plane_environment(values)
    audit_key_hex = values.get("MDI_CONTROL_PLANE_AUDIT_KEY_HEX", "").strip()
    bootstrap_raw = values.get("MDI_CONTROL_PLANE_BOOTSTRAP_JSON", "").strip()
    if not audit_key_hex:
        if bootstrap_raw:
            raise ValueError(
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX is required when bootstrap JSON is configured"
            )
        return None
    audit_key = _decode_audit_key(audit_key_hex)
    filename = _safe_database_filename(
        values.get("MDI_CONTROL_PLANE_FILENAME", "control-plane.sqlite3")
    )
    store = SQLiteControlPlaneStore(settings.storage_root / filename, audit_key=audit_key)

    bootstrap = _bootstrap_request(bootstrap_raw) if bootstrap_raw else None
    try:
        state = store.load_state()
    except ControlPlaneStateError as error:
        if "not initialized" not in str(error) or bootstrap is None:
            raise
        state = _state_from_bootstrap(bootstrap)
        store.bootstrap(
            state,
            audit=AuditContext(
                timestamp=utc_now(),
                correlation_id=f"corr_bootstrap_{uuid4().hex[:20]}",
                direct_peer="local_process",
                principal_id="local_bootstrap",
                token_id="local_bootstrap",
                action="security.bootstrap",
                outcome="accepted",
                details={
                    "generation": state.current.generation,
                    "record_count": len(state.current.records),
                },
            ),
        )
    else:
        if bootstrap is not None:
            raise ValueError(
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON must be removed after initialization"
            )

    limits = ControlPlaneLimits(
        max_header_bytes=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_MAX_HEADER_BYTES",
            default=32 * 1024,
            minimum=1024,
            maximum=1024 * 1024,
        ),
        max_body_bytes=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_MAX_BODY_BYTES",
            default=2 * 1024 * 1024,
            minimum=1024,
            maximum=64 * 1024 * 1024,
        ),
        max_sse_global=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_MAX_SSE_GLOBAL",
            default=32,
            minimum=1,
            maximum=4096,
        ),
        max_sse_per_principal=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_MAX_SSE_PER_PRINCIPAL",
            default=4,
            minimum=1,
            maximum=1024,
        ),
        max_sse_per_peer=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_MAX_SSE_PER_PEER",
            default=8,
            minimum=1,
            maximum=1024,
        ),
    )
    max_rate_entries = _bounded_int(
        values,
        "MDI_CONTROL_PLANE_RATE_MAX_ENTRIES",
        default=4096,
        minimum=16,
        maximum=1_000_000,
    )
    peer_limiter = BoundedTokenBucketLimiter(
        capacity=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_PEER_RATE_CAPACITY",
            default=120,
            minimum=1,
            maximum=100_000,
        ),
        refill_per_second=_bounded_float(
            values,
            "MDI_CONTROL_PLANE_PEER_RATE_REFILL",
            default=2.0,
            minimum=0.001,
            maximum=100_000.0,
        ),
        max_entries=max_rate_entries,
    )
    principal_limiter = BoundedTokenBucketLimiter(
        capacity=_bounded_int(
            values,
            "MDI_CONTROL_PLANE_PRINCIPAL_RATE_CAPACITY",
            default=60,
            minimum=1,
            maximum=100_000,
        ),
        refill_per_second=_bounded_float(
            values,
            "MDI_CONTROL_PLANE_PRINCIPAL_RATE_REFILL",
            default=1.0,
            minimum=0.001,
            maximum=100_000.0,
        ),
        max_entries=max_rate_entries,
    )
    return ControlPlaneSecurityController(
        authenticator=ReloadableApiTokenAuthenticator(state),
        store=store,
        limits=limits,
        peer_limiter=peer_limiter,
        principal_limiter=principal_limiter,
    )


def validate_control_plane_environment(environment: Mapping[str, str]) -> None:
    """Validate hardened-auth configuration without opening or bootstrapping state."""
    values = environment
    audit_key_hex = values.get("MDI_CONTROL_PLANE_AUDIT_KEY_HEX", "").strip()
    bootstrap_raw = values.get("MDI_CONTROL_PLANE_BOOTSTRAP_JSON", "").strip()
    if not audit_key_hex:
        if bootstrap_raw:
            raise ValueError(
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX is required when bootstrap JSON is configured"
            )
        return
    _decode_audit_key(audit_key_hex)
    _safe_database_filename(values.get("MDI_CONTROL_PLANE_FILENAME", "control-plane.sqlite3"))
    if bootstrap_raw:
        request = _bootstrap_request(bootstrap_raw)
        if request.overlap_seconds != 0:
            raise ValueError("bootstrap token overlap must be zero")
        _state_from_bootstrap(request)
    for name, default, minimum, maximum in (
        ("MDI_CONTROL_PLANE_MAX_HEADER_BYTES", 32 * 1024, 1024, 1024 * 1024),
        ("MDI_CONTROL_PLANE_MAX_BODY_BYTES", 2 * 1024 * 1024, 1024, 64 * 1024 * 1024),
        ("MDI_CONTROL_PLANE_MAX_SSE_GLOBAL", 32, 1, 4096),
        ("MDI_CONTROL_PLANE_MAX_SSE_PER_PRINCIPAL", 4, 1, 1024),
        ("MDI_CONTROL_PLANE_MAX_SSE_PER_PEER", 8, 1, 1024),
        ("MDI_CONTROL_PLANE_RATE_MAX_ENTRIES", 4096, 16, 1_000_000),
        ("MDI_CONTROL_PLANE_PEER_RATE_CAPACITY", 120, 1, 100_000),
        ("MDI_CONTROL_PLANE_PRINCIPAL_RATE_CAPACITY", 60, 1, 100_000),
    ):
        _bounded_int(values, name, default=default, minimum=minimum, maximum=maximum)
    _bounded_float(
        values,
        "MDI_CONTROL_PLANE_PEER_RATE_REFILL",
        default=2.0,
        minimum=0.001,
        maximum=100_000.0,
    )
    _bounded_float(
        values,
        "MDI_CONTROL_PLANE_PRINCIPAL_RATE_REFILL",
        default=1.0,
        minimum=0.001,
        maximum=100_000.0,
    )


def _bootstrap_request(raw: str) -> TokenReloadRequest:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("MDI_CONTROL_PLANE_BOOTSTRAP_JSON must contain valid JSON") from error
    return TokenReloadRequest.model_validate(parsed)


def _state_from_bootstrap(request: TokenReloadRequest) -> TokenSnapshotState:
    if request.overlap_seconds != 0:
        raise ValueError("bootstrap token overlap must be zero")
    now = utc_now()
    return TokenSnapshotState(
        current=ApiTokenSnapshot(
            generation=request.generation,
            created_at=now,
            records=tuple(token.to_record(default_not_before=now) for token in request.tokens),
        )
    )


def _decode_audit_key(value: str) -> bytes:
    if len(value) % 2:
        raise ValueError(
            "MDI_CONTROL_PLANE_AUDIT_KEY_HEX must contain an even number of hex digits"
        )
    try:
        decoded = bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("MDI_CONTROL_PLANE_AUDIT_KEY_HEX must contain hexadecimal data") from error
    if len(decoded) < 32:
        raise ValueError("MDI_CONTROL_PLANE_AUDIT_KEY_HEX must decode to at least 32 bytes")
    return decoded


def _safe_database_filename(value: str) -> str:
    normalized = value.strip()
    path = Path(normalized)
    if (
        not normalized
        or path.is_absolute()
        or len(path.parts) != 1
        or path.name in {".", ".."}
        or path.suffix not in {".sqlite", ".sqlite3", ".db"}
    ):
        raise ValueError("control-plane filename must be a safe local SQLite filename")
    return normalized


def _bounded_int(
    values: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    try:
        parsed = default if raw is None else int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(
    values: Mapping[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(name)
    try:
        parsed = default if raw is None else float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
