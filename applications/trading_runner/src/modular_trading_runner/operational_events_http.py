from __future__ import annotations

import hmac
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .operational_events import (
    OperationalEventQueryError,
    OperationalEventQueryErrorCode,
    OperationalSeverity,
    StructuredOperationalEventPage,
    TradingRunnerOperationalEventQuery,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_QUERY_PATH = "/events/v1/query"
_ALLOWED_QUERY_KEYS = frozenset({"cursor", "limit", "event_type", "severity"})
_MAX_QUERY_LENGTH = 4096


class TradingRunnerOperationalEventsHttpServer:
    """Reader-only loopback transport for sanitized owner operational events."""

    def __init__(
        self,
        query: TradingRunnerOperationalEventQuery,
        *,
        reader_token: str,
        host: str = "127.0.0.1",
        port: int = 8782,
    ) -> None:
        if not isinstance(query, TradingRunnerOperationalEventQuery):
            raise TypeError("query must be TradingRunnerOperationalEventQuery")
        normalized_host = host.strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("Trading Runner events server may bind only to loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Trading Runner events port must be between 0 and 65535")
        _token(reader_token)
        self._query = query
        self._authorization = f"Bearer {reader_token}".encode()
        current = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, body = current.response(
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                )
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((normalized_host, port), Handler)
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Trading Runner events server is closed")
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="trading-runner-operational-events",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()
        self._closed = True

    def response(
        self,
        *,
        path: str,
        authorization: str,
    ) -> tuple[int, dict[str, str], bytes]:
        if not self._authorized(authorization):
            return self._json(401, {"error": "UNAUTHORIZED"})
        if not isinstance(path, str) or len(path) > _MAX_QUERY_LENGTH:
            return self._json(400, {"error": "INVALID_REQUEST"})
        try:
            split = urllib.parse.urlsplit(path)
            if split.path != _QUERY_PATH or split.fragment:
                return self._json(404, {"error": "NOT_FOUND"})
            parameters = urllib.parse.parse_qs(
                split.query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=64,
            )
            if set(parameters) - _ALLOWED_QUERY_KEYS:
                raise ValueError("unknown query parameter")
            cursor = _single_optional(parameters, "cursor")
            limit_text = _single_optional(parameters, "limit")
            limit = 100 if limit_text is None else int(limit_text)
            event_types = _multiple(parameters, "event_type")
            severity_values = _multiple(parameters, "severity")
            severities = (
                tuple(OperationalSeverity(value) for value in severity_values)
                if severity_values is not None
                else None
            )
            page = self._query.query(
                cursor=cursor,
                limit=limit,
                event_types=event_types,
                severities=severities,
            )
        except OperationalEventQueryError as exc:
            return self._json(_error_status(exc.code), {"error": exc.code.value})
        except (TypeError, ValueError):
            return self._json(400, {"error": "INVALID_REQUEST"})
        return self._json(200, _page(page))

    def _authorized(self, supplied: str) -> bool:
        try:
            candidate = supplied.encode()
        except Exception:
            return False
        return hmac.compare_digest(candidate, self._authorization)

    @staticmethod
    def _json(
        status: int,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body


def _single_optional(parameters: dict[str, list[str]], name: str) -> str | None:
    values = parameters.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise ValueError(f"{name} must occur once and be non-empty")
    return values[0]


def _multiple(parameters: dict[str, list[str]], name: str) -> tuple[str, ...] | None:
    values = parameters.get(name)
    if values is None:
        return None
    if not values or any(not value for value in values):
        raise ValueError(f"{name} must be non-empty")
    return tuple(values)


def _page(page: StructuredOperationalEventPage) -> dict[str, object]:
    return {
        "source": page.source,
        "events": [
            {
                "event_id": event.event_id,
                "source": event.source,
                "resource": event.resource,
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "severity": event.severity.value,
                "state": event.state,
                "payload": dict(event.payload),
                "cursor": event.cursor,
            }
            for event in page.events
        ],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "scanned_count": page.scanned_count,
        "omitted_count": page.omitted_count,
        "retention_limit": page.retention_limit,
        "truncated": page.truncated,
    }


def _token(value: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError("reader_token must contain 16..4096 characters")


def _error_status(code: OperationalEventQueryErrorCode) -> int:
    if code in {
        OperationalEventQueryErrorCode.INVALID_REQUEST,
        OperationalEventQueryErrorCode.INVALID_CURSOR,
    }:
        return 400
    if code is OperationalEventQueryErrorCode.SOURCE_UNAVAILABLE:
        return 503
    return 500
