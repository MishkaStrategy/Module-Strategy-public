from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPConnection
from urllib.parse import urlsplit

from ..contracts import ComponentHealth, ComponentId, HealthStatus, SafeScalar
from ..service import Clock, utc_now

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_HEALTH_BYTES = 64 * 1024
_MAX_ERROR_SYMBOLS = 4096


@dataclass(frozen=True, slots=True)
class SignalBotHealthSnapshot:
    status: str
    cycles: int
    pairs_processed: int
    signals_sent: int
    pair_errors: int
    last_cycle_at: str | None
    last_success_at: str | None
    error_symbol_count: int


class SignalBotHttpHealthReader:
    """Read the existing dependency-free Signal Bot `/health` endpoint over loopback only."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 2.0) -> None:
        self._base_url = validate_signal_bot_base_url(base_url)
        if not 0.05 <= timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds must be between 0.05 and 30")
        self._timeout_seconds = timeout_seconds
        parsed = urlsplit(self._base_url)
        assert parsed.hostname is not None
        self._host = parsed.hostname
        self._port = parsed.port or 80

    def health(self) -> SignalBotHealthSnapshot:
        connection = HTTPConnection(self._host, self._port, timeout=self._timeout_seconds)
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(_MAX_HEALTH_BYTES + 1)
        finally:
            connection.close()
        if len(body) > _MAX_HEALTH_BYTES:
            raise ValueError("Signal Bot health response exceeds the bounded payload limit")
        if response.status not in {200, 503}:
            raise ValueError("Signal Bot health endpoint returned an unsupported HTTP status")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Signal Bot health endpoint returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("Signal Bot health payload must be an object")
        source_status = _status(payload.get("status"))
        expected_http = 200 if source_status == "ok" else 503
        if response.status != expected_http:
            raise ValueError("Signal Bot health HTTP status is inconsistent with its payload")
        metrics = payload.get("metrics")
        if not isinstance(metrics, Mapping):
            raise ValueError("Signal Bot health metrics must be an object")
        errors_by_symbol = metrics.get("errors_by_symbol", {})
        if not isinstance(errors_by_symbol, Mapping):
            raise ValueError("Signal Bot errors_by_symbol must be an object")
        if len(errors_by_symbol) > _MAX_ERROR_SYMBOLS:
            raise ValueError("Signal Bot errors_by_symbol exceeds the bounded entry limit")
        return SignalBotHealthSnapshot(
            status=source_status,
            cycles=_nonnegative_int(metrics.get("cycles"), "cycles"),
            pairs_processed=_nonnegative_int(
                metrics.get("pairs_processed"),
                "pairs_processed",
            ),
            signals_sent=_nonnegative_int(metrics.get("signals_sent"), "signals_sent"),
            pair_errors=_nonnegative_int(metrics.get("pair_errors"), "pair_errors"),
            last_cycle_at=_optional_timestamp(metrics.get("last_cycle_at"), "last_cycle_at"),
            last_success_at=_optional_timestamp(
                metrics.get("last_success_at"),
                "last_success_at",
            ),
            error_symbol_count=len(errors_by_symbol),
        )


class SignalBotHealthAdapter:
    component = ComponentId.SIGNAL_BOT

    def __init__(
        self,
        reader: SignalBotHttpHealthReader,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not isinstance(reader, SignalBotHttpHealthReader):
            raise TypeError("reader must be SignalBotHttpHealthReader")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    def health(self) -> ComponentHealth:
        snapshot = self._reader.health()
        status = HealthStatus.HEALTHY if snapshot.status == "ok" else HealthStatus.DEGRADED
        details: dict[str, SafeScalar] = {
            "cycles": snapshot.cycles,
            "pairs_processed": snapshot.pairs_processed,
            "signals_sent": snapshot.signals_sent,
            "pair_errors": snapshot.pair_errors,
            "error_symbol_count": snapshot.error_symbol_count,
            "last_cycle_at": snapshot.last_cycle_at,
            "last_success_at": snapshot.last_success_at,
            "test_notification_available": False,
        }
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=self._checked_at(),
            summary=(
                "Signal Bot loopback health is healthy"
                if status is HealthStatus.HEALTHY
                else "Signal Bot loopback health is degraded"
            ),
            details=details,
        )

    def _checked_at(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value


def validate_signal_bot_base_url(base_url: str) -> str:
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("Signal Bot health URL must use HTTP on the local loopback boundary")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("Signal Bot health URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Signal Bot health URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Signal Bot health URL must be a pathless loopback base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Signal Bot health URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Signal Bot health URL port must be between 1 and 65535")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}{f':{port}' if port is not None else ''}"


def _status(value: object) -> str:
    if not isinstance(value, str) or value not in {"ok", "degraded"}:
        raise ValueError("Signal Bot health status must be ok or degraded")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Signal Bot {name} must be a non-negative integer")
    return value


def _optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"Signal Bot {name} must be a bounded timestamp string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Signal Bot {name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Signal Bot {name} must be timezone-aware")
    return value
