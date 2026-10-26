from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

from .acceptance_query import TradingRuntimeAcceptanceSnapshot

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ACCEPTANCE_PATH = "/lifecycle/v1/acceptance"
_MAX_RESPONSE_BYTES = 8192
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_FIELDS = frozenset(
    {
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "configuration_hash",
        "native_state",
        "ready",
        "live",
        "component_count",
        "critical_failure_count",
    }
)


class TradingRunnerAcceptanceHttpQuery:
    """Strict reader-only client for the singleton Trading Runner acceptance projection."""

    def __init__(
        self,
        base_url: str,
        *,
        reader_token: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = _loopback_base_url(base_url)
        self._reader_token = _token(reader_token, "reader_token")
        self._timeout_seconds = _timeout(timeout_seconds)

    def snapshot(self) -> TradingRuntimeAcceptanceSnapshot:
        request = urllib.request.Request(
            f"{self._base_url}{_ACCEPTANCE_PATH}",
            headers={"Authorization": f"Bearer {self._reader_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise RuntimeError("Trading Runner acceptance snapshot is unavailable")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Trading Runner acceptance snapshot is unavailable") from None
        if len(body) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("Trading Runner acceptance snapshot is invalid")
        return _decode_snapshot(body)


def _decode_snapshot(body: bytes) -> TradingRuntimeAcceptanceSnapshot:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Trading Runner acceptance snapshot is invalid") from None
    if not isinstance(payload, dict) or set(payload) != _SNAPSHOT_FIELDS:
        raise RuntimeError("Trading Runner acceptance snapshot is invalid")

    strings: dict[str, str] = {}
    for name in (
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "native_state",
    ):
        value = payload[name]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise RuntimeError("Trading Runner acceptance snapshot is invalid")
        strings[name] = value
    configuration_hash = payload["configuration_hash"]
    if not isinstance(configuration_hash, str) or _SHA256_RE.fullmatch(configuration_hash) is None:
        raise RuntimeError("Trading Runner acceptance snapshot is invalid")
    ready = payload["ready"]
    live = payload["live"]
    if not isinstance(ready, bool) or not isinstance(live, bool):
        raise RuntimeError("Trading Runner acceptance snapshot is invalid")
    component_count = payload["component_count"]
    critical_failure_count = payload["critical_failure_count"]
    for value in (component_count, critical_failure_count):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("Trading Runner acceptance snapshot is invalid")
    if component_count < critical_failure_count:
        raise RuntimeError("Trading Runner acceptance snapshot is invalid")
    if strings["execution_mode"] != "paper":
        raise RuntimeError("Trading Runner acceptance snapshot is invalid")

    return TradingRuntimeAcceptanceSnapshot(
        runtime_id=strings["runtime_id"],
        portfolio_id=strings["portfolio_id"],
        strategy_id=strings["strategy_id"],
        execution_mode=strings["execution_mode"],
        base_timeframe=strings["base_timeframe"],
        configuration_hash=configuration_hash,
        native_state=strings["native_state"],
        ready=ready,
        live=live,
        component_count=component_count,
        critical_failure_count=critical_failure_count,
    )


def _loopback_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("base_url must be a loopback HTTP origin")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.hostname.lower() not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be a loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("base_url must be a loopback HTTP origin") from None
    if port is None or not 1 <= port <= 65535:
        raise ValueError("base_url must include a valid port")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{host}:{port}"


def _token(value: str, name: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")
    return value


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be between 0.1 and 60")
    rendered = float(value)
    if not math.isfinite(rendered) or not 0.1 <= rendered <= 60.0:
        raise ValueError("timeout_seconds must be between 0.1 and 60")
    return rendered


__all__ = ["TradingRunnerAcceptanceHttpQuery"]
