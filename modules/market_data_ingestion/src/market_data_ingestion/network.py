from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .errors import AdapterError
from .version import PACKAGE_VERSION


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "MALFORMED_JSON",
                "official endpoint returned malformed JSON",
                False,
                "download",
                {"url": self.url, "status": self.status},
            ) from exc


class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: float) -> None:
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("token bucket values must be positive")
        self._rate = rate_per_second
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        if tokens <= 0 or tokens > self._capacity:
            raise ValueError("invalid token request")
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                delay = (tokens - self._tokens) / self._rate
            time.sleep(delay)
            waited += delay


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, reset_seconds: float = 30.0) -> None:
        if failure_threshold <= 0 or reset_seconds <= 0:
            raise ValueError("invalid circuit breaker settings")
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def before_request(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at >= self._reset_seconds:
                self._opened_at = None
                self._failures = 0
                return
            raise AdapterError(
                "CIRCUIT_OPEN",
                "exchange adapter circuit breaker is open",
                True,
                "download",
            )

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = time.monotonic()


Transport = Callable[[str, float, Mapping[str, str]], HttpResponse]


class _Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...


def _read_bounded(handle: _Readable, maximum: int, *, url: str) -> bytes:
    body = handle.read(maximum + 1)
    if len(body) > maximum:
        raise AdapterError(
            "RESPONSE_SIZE_LIMIT",
            "official endpoint response exceeds the configured byte limit",
            False,
            "download",
            {"url": url, "maximum_bytes": maximum},
        )
    return body


def _default_transport(
    url: str,
    timeout: float,
    headers: Mapping[str, str],
    *,
    max_response_bytes: int,
) -> HttpResponse:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            return HttpResponse(
                status=int(response.status),
                headers={key.lower(): value for key, value in response.headers.items()},
                body=_read_bounded(response, max_response_bytes, url=final_url),
                url=final_url,
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=_read_bounded(exc, max_response_bytes, url=url),
            url=url,
        )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        retries: int = 5,
        bucket: TokenBucket | None = None,
        breaker: CircuitBreaker | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
        max_response_bytes: int = 2_100_000_000,
    ) -> None:
        if timeout_seconds <= 0 or retries < 0:
            raise ValueError("invalid HTTP client settings")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        self._timeout = timeout_seconds
        self._retries = retries
        self._max_response_bytes = max_response_bytes
        self._bucket = bucket or TokenBucket(5.0, 10.0)
        self._breaker = breaker or CircuitBreaker()
        self._transport = transport or (
            lambda url, timeout, headers: _default_transport(
                url,
                timeout,
                headers,
                max_response_bytes=self._max_response_bytes,
            )
        )
        self._sleep = sleep
        self._random = random_source or random.Random()
        self._random_lock = threading.Lock()

    def _jitter(self) -> float:
        with self._random_lock:
            return self._random.uniform(0.0, 0.25)

    def get(
        self,
        base_url: str,
        path: str = "",
        params: Mapping[str, object] | None = None,
    ) -> HttpResponse:
        query = urllib.parse.urlencode({key: str(value) for key, value in (params or {}).items()})
        url = base_url.rstrip("/") + "/" + path.lstrip("/") if path else base_url
        if query:
            url += ("&" if "?" in url else "?") + query
        safe_url = (
            urllib.parse.urlsplit(url)._replace(query="<redacted>").geturl() if query else url
        )
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            self._breaker.before_request()
            self._bucket.acquire()
            try:
                response = self._transport(
                    url,
                    self._timeout,
                    {
                        "Accept": "application/json,application/zip,text/plain",
                        "User-Agent": (f"modular-market-data-ingestion/{PACKAGE_VERSION}"),
                    },
                )
                if len(response.body) > self._max_response_bytes:
                    raise AdapterError(
                        "RESPONSE_SIZE_LIMIT",
                        "official endpoint response exceeds the configured byte limit",
                        False,
                        "download",
                        {"url": safe_url, "maximum_bytes": self._max_response_bytes},
                    )
            except (TimeoutError, ConnectionError, OSError) as exc:
                self._breaker.failure()
                last_error = exc
                if attempt >= self._retries:
                    break
                self._sleep(min(30.0, 0.25 * (2**attempt)) + self._jitter())
                continue
            if 200 <= response.status < 300:
                self._breaker.success()
                return response
            if response.status in {429, 418} or 500 <= response.status < 600:
                self._breaker.failure()
                if attempt >= self._retries:
                    last_error = AdapterError(
                        "HTTP_RETRY_EXHAUSTED",
                        f"official endpoint returned HTTP {response.status}",
                        True,
                        "download",
                        {"url": safe_url, "status": response.status},
                    )
                    break
                delay = _retry_after(response.headers)
                if delay is None:
                    delay = min(30.0, 0.25 * (2**attempt)) + self._jitter()
                self._sleep(delay)
                continue
            raise AdapterError(
                "HTTP_ERROR",
                f"official endpoint returned HTTP {response.status}",
                False,
                "download",
                {"url": safe_url, "status": response.status},
            )
        if isinstance(last_error, AdapterError):
            raise last_error
        raise AdapterError(
            "NETWORK_RETRY_EXHAUSTED",
            "official endpoint could not be reached after bounded retries",
            True,
            "download",
            {"url": safe_url, "reason": type(last_error).__name__ if last_error else "unknown"},
        ) from last_error
