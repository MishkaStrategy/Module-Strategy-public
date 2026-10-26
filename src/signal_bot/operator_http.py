from __future__ import annotations

import hmac
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .operator_contract import (
    SignalBotControlError,
    SignalBotControlErrorCode,
    TestNotificationRequest,
    TrustedSignalBotOperator,
)
from .operator_control import SignalBotOperatorControl

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_BODY_BYTES = 4096
_STATUS_PATH = "/control/v1/status"
_TEST_PATH = "/control/v1/test-notification"


class SignalBotControlHttpServer:
    """Loopback-only authenticated HTTP transport for the owner control boundary."""

    def __init__(
        self,
        control: SignalBotOperatorControl,
        *,
        reader_token: str,
        operator_token: str,
        host: str = "127.0.0.1",
        port: int = 8081,
        operator_subject: str = "control-plane:local-operator",
    ) -> None:
        if not isinstance(control, SignalBotOperatorControl):
            raise TypeError("control must be SignalBotOperatorControl")
        normalized_host = host.strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("Signal Bot control server may bind only to loopback")
        if not 0 <= port <= 65535:
            raise ValueError("Signal Bot control port must be between 0 and 65535")
        _validate_token(reader_token, "reader_token")
        _validate_token(operator_token, "operator_token")
        if hmac.compare_digest(reader_token.encode(), operator_token.encode()):
            raise ValueError("reader_token and operator_token must be distinct")
        self._control = control
        self._reader_authorization = f"Bearer {reader_token}".encode()
        self._operator_authorization = f"Bearer {operator_token}".encode()
        self._operator = TrustedSignalBotOperator(operator_subject)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, body = owner.response(
                    method="GET",
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                )
                self._write(status, headers, body)

            def do_POST(self) -> None:
                content_length = self.headers.get("Content-Length")
                if content_length is None:
                    self._write(*owner._json_response(411, {"error": "LENGTH_REQUIRED"}))
                    return
                try:
                    length = int(content_length)
                except ValueError:
                    self._write(*owner._json_response(400, {"error": "INVALID_REQUEST"}))
                    return
                if length < 0 or length > _MAX_BODY_BYTES:
                    self._write(*owner._json_response(413, {"error": "REQUEST_TOO_LARGE"}))
                    return
                body = self.rfile.read(length)
                status, headers, response_body = owner.response(
                    method="POST",
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                    body=body,
                )
                self._write(status, headers, response_body)

            def _write(self, status: int, headers: dict[str, str], body: bytes) -> None:
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

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="signal-bot-operator-control",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def response(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        body: bytes = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and path == _STATUS_PATH:
            if not self._authorized(authorization, self._reader_authorization):
                return self._json_response(401, {"error": "UNAUTHORIZED"})
            status = self._control.status()
            return self._json_response(
                200,
                {
                    "configured": status.configured,
                    "reachable": status.reachable,
                    "notifications_enabled": status.notifications_enabled,
                    "status": status.status,
                    "cycles": status.cycles,
                    "signals_sent": status.signals_sent,
                    "last_success_at": _timestamp(status.last_success_at),
                    "last_error_type": status.last_error_type,
                },
            )
        if method == "POST" and path == _TEST_PATH:
            if not self._authorized(authorization, self._operator_authorization):
                return self._json_response(401, {"error": "UNAUTHORIZED"})
            if len(body) > _MAX_BODY_BYTES:
                return self._json_response(413, {"error": "REQUEST_TOO_LARGE"})
            try:
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"request_id"}:
                    raise ValueError("invalid request object")
                request_id = payload["request_id"]
                if not isinstance(request_id, str):
                    raise ValueError("request_id must be a string")
                request = TestNotificationRequest(
                    request_id=request_id,
                    operator=self._operator,
                )
                result = self._control.test_notification(request)
            except SignalBotControlError as exc:
                return self._json_response(
                    _error_status(exc.code),
                    {"error": exc.code.value, "request_id": exc.request_id},
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                return self._json_response(400, {"error": "INVALID_REQUEST"})
            return self._json_response(
                200,
                {
                    "request_id": result.request_id,
                    "operator": result.operator,
                    "completed_at": result.completed_at.isoformat(),
                    "replayed": result.replayed,
                },
            )
        return self._json_response(404, {"error": "NOT_FOUND"})

    @staticmethod
    def _authorized(supplied: str, expected: bytes) -> bool:
        try:
            candidate = supplied.encode()
        except Exception:
            return False
        return hmac.compare_digest(candidate, expected)

    @staticmethod
    def _json_response(
        status: int, payload: dict[str, object]
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body


def _validate_token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 1..4096 characters")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _error_status(code: SignalBotControlErrorCode) -> int:
    if code is SignalBotControlErrorCode.INVALID_REQUEST:
        return 400
    if code is SignalBotControlErrorCode.IDEMPOTENCY_CONFLICT:
        return 409
    if code is SignalBotControlErrorCode.RATE_LIMITED:
        return 429
    if code is SignalBotControlErrorCode.REJECTED:
        return 502
    if code in {
        SignalBotControlErrorCode.NOT_CONFIGURED,
        SignalBotControlErrorCode.UNREACHABLE,
        SignalBotControlErrorCode.RECOVERY_REQUIRED,
        SignalBotControlErrorCode.OWNER_FAILURE,
    }:
        return 503
    return 500
