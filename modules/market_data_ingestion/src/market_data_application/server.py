from __future__ import annotations

import argparse
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI

from .api import create_api
from .backfill_application import MarketDataApplication
from .composition import build_application_context
from .control_plane_http import ControlPlaneSecurityController
from .control_plane_runtime import (
    control_plane_from_environment,
    validate_control_plane_environment,
)
from .security import ApiTokenAuthenticator
from .settings import ApplicationSettings


def settings_from_environment(environment: Mapping[str, str] | None = None) -> ApplicationSettings:
    values = os.environ if environment is None else environment
    return ApplicationSettings(
        storage_root=Path(values.get("MDI_STORAGE_ROOT", "./data")),
        registry_filename=values.get("MDI_REGISTRY_FILENAME", "registry.sqlite3"),
        jobs_filename=values.get("MDI_JOBS_FILENAME", "jobs.sqlite3"),
        network_timeout_seconds=float(values.get("MDI_NETWORK_TIMEOUT_SECONDS", "30")),
        network_retries=int(values.get("MDI_NETWORK_RETRIES", "6")),
        worker_count=int(values.get("MDI_WORKER_COUNT", "2")),
        instance_id=values.get(
            "MDI_INSTANCE_ID",
            f"mdi_api_{os.getpid()}_{uuid4().hex[:12]}",
        ),
        lease_ttl_seconds=float(values.get("MDI_LEASE_TTL_SECONDS", "30")),
        lease_heartbeat_seconds=float(values.get("MDI_LEASE_HEARTBEAT_SECONDS", "5")),
        include_fake_adapter=_boolean(values.get("MDI_INCLUDE_FAKE_ADAPTER", "false")),
    )


def authenticator_from_environment(
    environment: Mapping[str, str] | None = None,
) -> ApiTokenAuthenticator | None:
    values = os.environ if environment is None else environment
    raw = values.get("MDI_API_TOKENS_JSON", "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(token, str) and isinstance(role, str) for token, role in parsed.items()
    ):
        raise ValueError("MDI_API_TOKENS_JSON must be a JSON object of token-to-role strings")
    return ApiTokenAuthenticator(parsed)


def authentication_mode_from_environment(environment: Mapping[str, str]) -> str:
    """Validate one auth mode without constructing durable Control Plane state."""
    legacy_raw = environment.get("MDI_API_TOKENS_JSON", "").strip()
    audit_key = environment.get("MDI_CONTROL_PLANE_AUDIT_KEY_HEX", "").strip()
    bootstrap = environment.get("MDI_CONTROL_PLANE_BOOTSTRAP_JSON", "").strip()
    hardened_configured = bool(audit_key or bootstrap)
    if legacy_raw and hardened_configured:
        raise ValueError(
            "MDI_API_TOKENS_JSON and hardened control-plane authentication "
            "must not be configured together"
        )
    if legacy_raw:
        authenticator_from_environment(environment)
        return "legacy_bearer"
    if hardened_configured:
        validate_control_plane_environment(environment)
        return "hardened_control_plane"
    raise ValueError("Market Data API requires bearer or hardened Control Plane authentication")


def create_server(
    application: MarketDataApplication | None = None,
    *,
    authenticator: ApiTokenAuthenticator | None = None,
    control_plane: ControlPlaneSecurityController | None = None,
) -> FastAPI:
    if (authenticator is None) == (control_plane is None):
        raise ValueError("exactly one authentication provider must be configured")
    if authenticator is not None and not isinstance(authenticator, ApiTokenAuthenticator):
        raise TypeError("authenticator must be an ApiTokenAuthenticator")
    if control_plane is not None and not isinstance(control_plane, ControlPlaneSecurityController):
        raise TypeError("control_plane must be a ControlPlaneSecurityController")
    owned = application is None
    current = application or MarketDataApplication(
        build_application_context(settings_from_environment())
    )

    @asynccontextmanager
    async def lifespan(_api: FastAPI) -> AsyncIterator[None]:
        current.recover_jobs()
        try:
            yield
        finally:
            if owned:
                current.shutdown()

    return create_api(
        current,
        lifespan=lifespan,
        authenticator=authenticator,
        control_plane=control_plane,
    )


def validate_bind_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Market Data API may bind only to a loopback host")
    return normalized


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-data-api")
    parser.add_argument("--host", default=os.environ.get("MDI_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MDI_API_PORT", "8000")))
    parser.add_argument("--log-level", default=os.environ.get("MDI_API_LOG_LEVEL", "info"))
    parser.add_argument(
        "--check", action="store_true", help="Validate server configuration and exit."
    )
    args = parser.parse_args(argv)

    environment = os.environ
    authentication_mode = authentication_mode_from_environment(environment)
    authenticator = authenticator_from_environment(environment)
    host = validate_bind_host(args.host)
    if not 1 <= args.port <= 65535:
        raise ValueError("API port must be between 1 and 65535")
    settings = settings_from_environment(environment)
    if args.check:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "host": host,
                    "port": args.port,
                    "storage_root": str(settings.storage_root),
                    "instance_id": settings.instance_id,
                    "authentication_enabled": True,
                    "authentication_mode": authentication_mode,
                    "remote_bind_allowed": False,
                },
                sort_keys=True,
            )
        )
        return 0

    control_plane = (
        control_plane_from_environment(settings, environment)
        if authentication_mode == "hardened_control_plane"
        else None
    )
    authenticator = None if authentication_mode == "hardened_control_plane" else authenticator
    application = MarketDataApplication(build_application_context(settings))
    uvicorn.run(
        create_server(
            application,
            authenticator=authenticator,
            control_plane=control_plane,
        ),
        host=host,
        port=args.port,
        log_level=args.log_level,
    )
    application.shutdown()
    return 0


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean environment values must be true or false")
