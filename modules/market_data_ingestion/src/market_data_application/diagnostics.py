from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from market_data_ingestion.doctor import doctor as backend_doctor
from market_data_ingestion.enums import Severity

from .application import MarketDataApplication as ProcessLocalMarketDataApplication
from .contracts import DoctorCheck, DoctorResponse, HealthResponse
from .transport_contracts import VersionResponse

_PACKAGE_NAME = "modular-market-data-ingestion"
_FALLBACK_VERSION = "0.4.0"


def build_version() -> str:
    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _FALLBACK_VERSION


def version_response() -> VersionResponse:
    return VersionResponse(build_version=build_version())


def health_response(application: ProcessLocalMarketDataApplication) -> HealthResponse:
    checks: dict[str, str] = {}
    status: Literal["ok", "degraded", "error"] = "ok"
    try:
        application.context.job_store.list_jobs(limit=1)
        checks["jobs"] = "ok"
    except Exception as exc:
        checks["jobs"] = f"error:{type(exc).__name__}"
        status = "error"
    try:
        registry = application.context.dataset_registry.health()
        registry_status = str(registry.get("status", "error"))
        checks["registry"] = registry_status
        if registry_status != "ok" and status == "ok":
            status = "degraded"
    except Exception as exc:
        checks["registry"] = f"error:{type(exc).__name__}"
        status = "error"
    checks["worker"] = application.context.settings.instance_id
    return HealthResponse(
        status=status,
        build_version=build_version(),
        checks=checks,
    )


def doctor_response(application: ProcessLocalMarketDataApplication) -> DoctorResponse:
    raw = backend_doctor(
        storage_root=application.context.settings.storage_root,
        registry=application.context.dataset_registry,
        adapters=application.context.adapters,
    )
    storage = raw.get("storage", {})
    network = raw.get("network", {})
    registry = raw.get("registry", {})
    storage_map = storage if isinstance(storage, dict) else {}
    network_map = network if isinstance(network, dict) else {}
    registry_map = registry if isinstance(registry, dict) else {}

    writable = storage_map.get("writable") is True
    free_bytes = int(storage_map.get("free_bytes", 0))
    minimum_free = int(storage_map.get("minimum_free_bytes", 0))
    storage_ok = writable and free_bytes > minimum_free
    registry_ok = registry_map.get("status") == "ok"
    dns_ok = network_map.get("dns_data_binance_vision") is True

    checks = (
        DoctorCheck(
            name="storage",
            status="ok" if storage_ok else "error",
            explanation=(
                f"storage writable={writable}, free_bytes={free_bytes}, "
                f"minimum_free_bytes={minimum_free}"
            ),
            remediation=None if storage_ok else "Free disk space and verify write permissions.",
            severity=Severity.INFO if storage_ok else Severity.CRITICAL,
        ),
        DoctorCheck(
            name="registry",
            status="ok" if registry_ok else "error",
            explanation=f"dataset registry status={registry_map.get('status', 'unknown')}",
            remediation=None if registry_ok else "Inspect or restore the Dataset Registry.",
            severity=Severity.INFO if registry_ok else Severity.CRITICAL,
        ),
        DoctorCheck(
            name="public_dns",
            status="ok" if dns_ok else "warning",
            explanation=f"data.binance.vision DNS available={dns_ok}",
            remediation=None if dns_ok else "Check DNS before public-network acceptance tests.",
            severity=Severity.INFO if dns_ok else Severity.WARNING,
        ),
    )
    status: Literal["ok", "degraded", "error"]
    if any(check.status == "error" for check in checks):
        status = "error"
    elif any(check.status == "warning" for check in checks):
        status = "degraded"
    else:
        status = "ok"
    return DoctorResponse(status=status, checks=checks)
