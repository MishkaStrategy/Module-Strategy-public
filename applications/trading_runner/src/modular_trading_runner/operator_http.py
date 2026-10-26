from __future__ import annotations

import asyncio
import hmac
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .acceptance_query import ReadOnlyTradingRuntimeAcceptanceQuery
from .operator_lifecycle import (
    LifecycleMutationError,
    LifecycleMutationErrorCode,
    LifecycleMutationRequest,
    LifecycleMutationResult,
    LifecycleOperation,
    TradingRunnerOperatorLifecycle,
    TrustedOperatorIdentity,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_BODY_BYTES = 4096
_ACCEPTANCE_PATH = "/lifecycle/v1/acceptance"
_OPERATION_PATHS = {
    "/lifecycle/v1/start": LifecycleOperation.START,
    "/lifecycle/v1/stop": LifecycleOperation.STOP,
    "/lifecycle/v1/kill-switch/activate": LifecycleOperation.ACTIVATE_KILL_SWITCH,
    "/lifecycle/v1/kill-switch/reset": LifecycleOperation.RESET_KILL_SWITCH,
}


class TradingRunnerLifecycleHttpServer:
    """Loopback-only authenticated transport for the singleton lifecycle owner."""

    def __init__(
        self,
        owner: TradingRunnerOperatorLifecycle,
        *,
        reader_token: str,
        operator_token: str,
        host: str = "127.0.0.1",
        port: int = 8780,
        operator_subject: str = "control-plane:local-operator",
        mutation_timeout_seconds: float = 30.0,
    ) -> None:
        if not isinstance(owner, TradingRunnerOperatorLifecycle):
            raise TypeError("owner must be TradingRunnerOperatorLifecycle")
        normalized_host = host.strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("Trading Runner lifecycle server may bind only to loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Trading Runner lifecycle port must be between 0 and 65535")
        _token(reader_token, "reader_token")
        _token(operator_token, "operator_token")
        if hmac.compare_digest(reader_token.encode(), operator_token.encode()):
            raise ValueError("reader_token and operator_token must be distinct")
        if (
            isinstance(mutation_timeout_seconds, bool)
            or not isinstance(mutation_timeout_seconds, (int, float))
            or not 0.1 <= float(mutation_timeout_seconds) <= 300.0
        ):
            raise ValueError("mutation_timeout_seconds must be between 0.1 and 300")
        self._owner = owner
        self._reader_authorization = f"Bearer {reader_token}".encode()
        self._operator_authorization = f"Bearer {operator_token}".encode()
        self._operator = TrustedOperatorIdentity(operator_subject)
        self._mutation_timeout_seconds = float(mutation_timeout_seconds)
        self._loop_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        current = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, body = current.response(
                    method="GET",
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                )
                self._write(status, headers, body)

            def do_POST(self) -> None:
                content_length = self.headers.get("Content-Length")
                if content_length is None:
                    self._write(*current._json(411, {"error": "LENGTH_REQUIRED"}))
                    return
                try:
                    length = int(content_length)
                except ValueError:
                    self._write(*current._json(400, {"error": "INVALID_REQUEST"}))
                    return
                if length < 0 or length > _MAX_BODY_BYTES:
                    self._write(*current._json(413, {"error": "REQUEST_TOO_LARGE"}))
                    return
                status, headers, body = current.response(
                    method="POST",
                    path=self.path,
                    authorization=self.headers.get("Authorization", ""),
                    body=self.rfile.read(length),
                )
                self._write(status, headers, body)

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
        self._server_thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server_thread is not None:
            return
        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name="trading-runner-lifecycle-control",
            daemon=True,
        )
        self._server_thread.start()

    def close(self) -> None:
        if self._server_thread is not None:
            self._server.shutdown()
            self._server_thread.join(timeout=5)
            self._server_thread = None
        self._server.server_close()
        with self._loop_lock:
            loop = self._loop
            loop_thread = self._loop_thread
            self._loop = None
            self._loop_thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if loop_thread is not None:
            loop_thread.join(timeout=5)
        if loop is not None and not loop.is_closed():
            loop.close()

    def response(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        body: bytes = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and path == _ACCEPTANCE_PATH:
            if body:
                return self._json(400, {"error": "INVALID_REQUEST"})
            if not self._authorized(authorization, self._reader_authorization):
                return self._json(401, {"error": "UNAUTHORIZED"})
            try:
                acceptance = ReadOnlyTradingRuntimeAcceptanceQuery(self._owner.runner)
                snapshot = acceptance.snapshot()
            except Exception:
                return self._json(503, {"error": "OWNER_FAILURE"})
            return self._json(
                200,
                {
                    "runtime_id": snapshot.runtime_id,
                    "portfolio_id": snapshot.portfolio_id,
                    "strategy_id": snapshot.strategy_id,
                    "execution_mode": snapshot.execution_mode,
                    "base_timeframe": snapshot.base_timeframe,
                    "configuration_hash": snapshot.configuration_hash,
                    "native_state": snapshot.native_state,
                    "ready": snapshot.ready,
                    "live": snapshot.live,
                    "component_count": snapshot.component_count,
                    "critical_failure_count": snapshot.critical_failure_count,
                },
            )

        operation = _OPERATION_PATHS.get(path)
        if method != "POST" or operation is None:
            return self._json(404, {"error": "NOT_FOUND"})
        if not self._authorized(authorization, self._operator_authorization):
            return self._json(401, {"error": "UNAUTHORIZED"})
        if len(body) > _MAX_BODY_BYTES:
            return self._json(413, {"error": "REQUEST_TOO_LARGE"})
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"request_id"}:
                raise ValueError("invalid lifecycle mutation object")
            request_id = payload["request_id"]
            if not isinstance(request_id, str):
                raise ValueError("request_id must be string")
            request = LifecycleMutationRequest(
                request_id=request_id,
                operator=self._operator,
                operation=operation,
            )
            result = self._execute(request)
        except LifecycleMutationError as exc:
            return self._json(
                _error_status(exc.code),
                {"error": exc.code.value, "request_id": exc.request_id},
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return self._json(400, {"error": "INVALID_REQUEST"})
        return self._json(
            200,
            {
                "request_id": result.request_id,
                "operator": result.operator,
                "operation": result.operation.value,
                "previous_state": result.previous_state.value,
                "new_state": result.new_state.value,
                "config_hash": result.config_hash,
                "completed_at": result.completed_at.isoformat(),
                "replayed": result.replayed,
            },
        )

    def _execute(self, request: LifecycleMutationRequest) -> LifecycleMutationResult:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._owner.execute(request), loop)
        try:
            return future.result(timeout=self._mutation_timeout_seconds)
        except FutureTimeoutError:
            raise LifecycleMutationError(
                LifecycleMutationErrorCode.RECOVERY_REQUIRED,
                "lifecycle mutation transport result is uncertain",
                request_id=request.request_id,
            ) from None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is not None and self._loop_thread is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=_run_event_loop,
                args=(loop,),
                name="trading-runner-lifecycle-owner-loop",
                daemon=True,
            )
            self._loop = loop
            self._loop_thread = thread
            thread.start()
            return loop

    @staticmethod
    def _authorized(supplied: str, expected: bytes) -> bool:
        try:
            candidate = supplied.encode()
        except Exception:
            return False
        return hmac.compare_digest(candidate, expected)

    @staticmethod
    def _json(
        status: int,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return status, {"Content-Type": "application/json", "Cache-Control": "no-store"}, body


def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")


def _error_status(code: LifecycleMutationErrorCode) -> int:
    if code in {LifecycleMutationErrorCode.INVALID_REQUEST, LifecycleMutationErrorCode.PAPER_ONLY}:
        return 400
    if code is LifecycleMutationErrorCode.IDEMPOTENCY_CONFLICT:
        return 409
    if code is LifecycleMutationErrorCode.INVALID_TRANSITION:
        return 422
    if code in {
        LifecycleMutationErrorCode.RECOVERY_REQUIRED,
        LifecycleMutationErrorCode.OWNER_FAILURE,
    }:
        return 503
    return 500
