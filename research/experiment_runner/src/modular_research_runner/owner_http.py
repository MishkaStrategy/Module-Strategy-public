from __future__ import annotations

import hmac
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .owner_identity import ResearchExperimentOwnerIdentity, read_experiment_owner_identity
from .runner import ResearchRunner

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_STATUS_PATH = "/owner/v1/research"
_EXECUTE_PATH = "/owner/v1/research/execute"
_MAX_BODY_BYTES = 128
_MAX_RESPONSE_BYTES = 8192
_EXPERIMENT_ID_RE = re.compile(r"^exp-[0-9a-f]{24}$")
_RESPONSE_FIELDS = frozenset({"experiment_id", "specification_sha256", "strategy_id", "status"})


@dataclass(frozen=True, slots=True)
class ResearchOwnerExecutionSummary:
    """Minimal structural result required by System Acceptance ResearchRunnerPort."""

    status: str


class _ResearchOwnerRepositoryClient:
    def __init__(self, client: ResearchRunnerOwnerHttpClient) -> None:
        self._client = client

    def experiment_status(self, experiment_id: str) -> str:
        self._client._require_experiment(experiment_id)
        return self._client._read_state()[1]


class ResearchRunnerOwnerHttpClient:
    """Strict client for one pre-authorized Research owner experiment."""

    def __init__(
        self,
        base_url: str,
        *,
        experiment_id: str,
        reader_token: str,
        operator_token: str,
        read_timeout_seconds: float = 5.0,
        execute_timeout_seconds: float = 21600.0,
    ) -> None:
        self._base_url = _loopback_base_url(base_url)
        self._experiment_id = _experiment_id(experiment_id)
        self._reader_token = _token(reader_token, "reader_token")
        self._operator_token = _token(operator_token, "operator_token")
        if hmac.compare_digest(self._reader_token.encode(), self._operator_token.encode()):
            raise ValueError("reader_token and operator_token must be distinct")
        self._read_timeout_seconds = _timeout(
            read_timeout_seconds,
            name="read_timeout_seconds",
            maximum=60.0,
        )
        self._execute_timeout_seconds = _timeout(
            execute_timeout_seconds,
            name="execute_timeout_seconds",
            maximum=86400.0,
        )
        self.repository = _ResearchOwnerRepositoryClient(self)

    def owner_identity(self) -> ResearchExperimentOwnerIdentity:
        return self._read_state()[0]

    def execute(self, experiment_id: str) -> ResearchOwnerExecutionSummary:
        self._require_experiment(experiment_id)
        payload = self._request(
            method="POST",
            path=_EXECUTE_PATH,
            authorization=f"Bearer {self._operator_token}",
            body=b"{}",
            timeout=self._execute_timeout_seconds,
            execution=True,
        )
        identity, status = _decode_state(payload)
        if identity.experiment_id != self._experiment_id:
            raise RuntimeError("Research owner identity changed")
        return ResearchOwnerExecutionSummary(status=status)

    def _read_state(self) -> tuple[ResearchExperimentOwnerIdentity, str]:
        payload = self._request(
            method="GET",
            path=_STATUS_PATH,
            authorization=f"Bearer {self._reader_token}",
            timeout=self._read_timeout_seconds,
        )
        identity, status = _decode_state(payload)
        if identity.experiment_id != self._experiment_id:
            raise RuntimeError("Research owner identity changed")
        return identity, status

    def _require_experiment(self, experiment_id: str) -> None:
        if experiment_id != self._experiment_id:
            raise ValueError("Research owner client is bound to a different experiment")

    def _request(
        self,
        *,
        method: str,
        path: str,
        authorization: str,
        timeout: float,
        body: bytes | None = None,
        execution: bool = False,
    ) -> bytes:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Authorization": authorization,
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError("Research owner response is unavailable")
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            message = (
                "Research owner execution outcome requires recovery"
                if execution
                else "Research owner response is unavailable"
            )
            raise RuntimeError(message) from None
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("Research owner response is invalid")
        return payload


class ResearchRunnerOwnerHttpServer:
    """Loopback transport over one already-existing ResearchRunner and experiment."""

    def __init__(
        self,
        runner: ResearchRunner,
        *,
        experiment_id: str,
        reader_token: str,
        operator_token: str,
        host: str = "127.0.0.1",
        port: int = 8782,
    ) -> None:
        if not isinstance(runner, ResearchRunner):
            raise TypeError("runner must be ResearchRunner")
        normalized_host = host.strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("Research owner server may bind only to loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("Research owner port must be between 0 and 65535")
        requested_experiment_id = _experiment_id(experiment_id)
        _token(reader_token, "reader_token")
        _token(operator_token, "operator_token")
        if hmac.compare_digest(reader_token.encode(), operator_token.encode()):
            raise ValueError("reader_token and operator_token must be distinct")

        self._runner = runner
        self._identity = read_experiment_owner_identity(runner.repository, requested_experiment_id)
        self._reader_authorization = f"Bearer {reader_token}".encode()
        self._operator_authorization = f"Bearer {operator_token}".encode()
        self._execute_lock = threading.Lock()
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
            name="research-runner-owner-control",
            daemon=True,
        )
        self._server_thread.start()

    def close(self) -> None:
        if self._server_thread is not None:
            self._server.shutdown()
            self._server_thread.join(timeout=5)
            self._server_thread = None
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
            if body:
                return self._json(400, {"error": "INVALID_REQUEST"})
            if not self._authorized(authorization, self._reader_authorization):
                return self._json(401, {"error": "UNAUTHORIZED"})
            return self._state_response()

        if method != "POST" or path != _EXECUTE_PATH:
            return self._json(404, {"error": "NOT_FOUND"})
        if not self._authorized(authorization, self._operator_authorization):
            return self._json(401, {"error": "UNAUTHORIZED"})
        if len(body) > _MAX_BODY_BYTES:
            return self._json(413, {"error": "REQUEST_TOO_LARGE"})
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "INVALID_REQUEST"})
        if not isinstance(payload, dict) or payload:
            return self._json(400, {"error": "INVALID_REQUEST"})

        with self._execute_lock:
            if self._current_identity() is None:
                return self._json(409, {"error": "OWNER_IDENTITY_CHANGED"})
            try:
                summary = self._runner.execute(self._identity.experiment_id)
                summary_status = _status(getattr(summary, "status", None))
                durable_status = _status(
                    self._runner.repository.experiment_status(self._identity.experiment_id)
                )
            except Exception:
                return self._json(503, {"error": "OWNER_FAILURE"})
            if summary_status != durable_status:
                return self._json(503, {"error": "RECOVERY_REQUIRED"})
            return self._json(200, self._state_payload(durable_status))

    def _state_response(self) -> tuple[int, dict[str, str], bytes]:
        if self._current_identity() is None:
            return self._json(409, {"error": "OWNER_IDENTITY_CHANGED"})
        try:
            status = _status(
                self._runner.repository.experiment_status(self._identity.experiment_id)
            )
        except Exception:
            return self._json(503, {"error": "OWNER_FAILURE"})
        return self._json(200, self._state_payload(status))

    def _current_identity(self) -> ResearchExperimentOwnerIdentity | None:
        try:
            current = read_experiment_owner_identity(
                self._runner.repository,
                self._identity.experiment_id,
            )
        except Exception:
            return None
        return current if current == self._identity else None

    def _state_payload(self, status: str) -> dict[str, object]:
        return {
            "experiment_id": self._identity.experiment_id,
            "specification_sha256": self._identity.specification_sha256,
            "strategy_id": self._identity.strategy_id,
            "status": status,
        }

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


def _decode_state(body: bytes) -> tuple[ResearchExperimentOwnerIdentity, str]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Research owner response is invalid") from None
    if not isinstance(payload, dict) or set(payload) != _RESPONSE_FIELDS:
        raise RuntimeError("Research owner response is invalid")
    try:
        identity = ResearchExperimentOwnerIdentity(
            experiment_id=payload["experiment_id"],
            specification_sha256=payload["specification_sha256"],
            strategy_id=payload["strategy_id"],
        )
    except (TypeError, ValueError, RuntimeError):
        raise RuntimeError("Research owner response is invalid") from None
    status = _status(payload["status"])
    return identity, status


def _status(value: object) -> str:
    rendered = value if isinstance(value, str) else getattr(value, "value", None)
    if not isinstance(rendered, str) or not rendered or len(rendered) > 64:
        raise RuntimeError("Research owner status is invalid")
    if (
        not rendered.isascii()
        or not rendered.replace("_", "").isalpha()
        or rendered.upper() != rendered
    ):
        raise RuntimeError("Research owner status is invalid")
    return rendered


def _experiment_id(value: str) -> str:
    if not isinstance(value, str) or _EXPERIMENT_ID_RE.fullmatch(value) is None:
        raise ValueError("experiment_id is invalid")
    return value


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


def _timeout(value: float, *, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} is invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or not 0.1 <= rendered <= maximum:
        raise ValueError(f"{name} is invalid")
    return rendered


__all__ = [
    "ResearchOwnerExecutionSummary",
    "ResearchRunnerOwnerHttpClient",
    "ResearchRunnerOwnerHttpServer",
]
