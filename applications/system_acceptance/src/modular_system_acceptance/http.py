from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceOwnerError,
    AcceptanceRunSnapshot,
    AcceptanceRunSpec,
    TrustedOperator,
)
from .owner import SystemAcceptanceOwner

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_BODY_BYTES = 16_384
_CURRENT_PATH = "/acceptance/v1/current"
_START_PATH = "/acceptance/v1/start"
_STOP_PATH = "/acceptance/v1/stop"
_CANCEL_PATH = "/acceptance/v1/cancel"
_RETRY_PATH = "/acceptance/v1/retry"
_MUTATION_PATHS = {
    _START_PATH: AcceptanceMutation.START,
    _STOP_PATH: AcceptanceMutation.STOP,
    _CANCEL_PATH: AcceptanceMutation.CANCEL,
    _RETRY_PATH: AcceptanceMutation.RETRY,
}


class SystemAcceptanceHttpServer:
    """Loopback-only owner transport with split reader/operator credentials."""

    def __init__(
        self,
        owner: SystemAcceptanceOwner,
        *,
        reader_token: str,
        operator_token: str,
        host: str = "127.0.0.1",
        port: int = 8781,
        operator_subject: str = "control-plane:local-operator",
    ) -> None:
        if not isinstance(owner, SystemAcceptanceOwner):
            raise TypeError("owner must be SystemAcceptanceOwner")
        normalized_host = host.strip().lower()
        if normalized_host not in _LOOPBACK_HOSTS:
            raise ValueError("System Acceptance server may bind only to loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("System Acceptance port must be between 0 and 65535")
        _token(reader_token, "reader_token")
        _token(operator_token, "operator_token")
        if hmac.compare_digest(reader_token.encode(), operator_token.encode()):
            raise ValueError("reader_token and operator_token must be distinct")
        self._owner = owner
        self._reader = f"Bearer {reader_token}".encode()
        self._operator = f"Bearer {operator_token}".encode()
        self._operator_subject = TrustedOperator(operator_subject)
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
                length_header = self.headers.get("Content-Length")
                if length_header is None:
                    self._write(*current._json(411, {"error": "LENGTH_REQUIRED"}))
                    return
                try:
                    length = int(length_header)
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
            name="system-acceptance-owner",
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
        if method == "GET" and path == _CURRENT_PATH:
            if not self._authorized(authorization, self._reader):
                return self._json(401, {"error": AcceptanceErrorCode.UNAUTHORIZED.value})
            snapshot = self._owner.snapshot()
            return self._json(200, {"run": _snapshot(snapshot)})
        if method == "POST" and path in _MUTATION_PATHS:
            if not self._authorized(authorization, self._operator):
                return self._json(401, {"error": AcceptanceErrorCode.UNAUTHORIZED.value})
            if len(body) > _MAX_BODY_BYTES:
                return self._json(413, {"error": "REQUEST_TOO_LARGE"})
            try:
                request = self._request(_MUTATION_PATHS[path], body)
                result = self._owner.mutate(request)
            except AcceptanceOwnerError as exc:
                return self._json(
                    _error_status(exc.code),
                    {
                        "error": exc.code.value,
                        "request_id": exc.request_id,
                        "acceptance_run_id": exc.run_id,
                    },
                )
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                return self._json(400, {"error": AcceptanceErrorCode.INVALID_REQUEST.value})
            return self._json(
                200,
                {
                    "request_id": result.request_id,
                    "operator": result.operator,
                    "mutation": result.mutation.value,
                    "acceptance_run_id": result.acceptance_run_id,
                    "status": result.status.value,
                    "phase": result.phase.value,
                    "completed_at": result.completed_at.isoformat(),
                    "replayed": result.replayed,
                },
            )
        return self._json(404, {"error": "NOT_FOUND"})

    def _request(self, mutation: AcceptanceMutation, body: bytes) -> AcceptanceMutationRequest:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be object")
        if mutation is AcceptanceMutation.START:
            if set(payload) != {"request_id", "spec"} or not isinstance(payload["spec"], dict):
                raise ValueError("START body is invalid")
            spec_raw = payload["spec"]
            allowed = {
                "full_rc_sha",
                "dataset_id",
                "dataset_digest",
                "strategy_id",
                "strategy_digest",
                "config_digest",
                "enable_soak_24h",
                "enable_soak_72h",
                "mode",
            }
            if set(spec_raw) - allowed:
                raise ValueError("START spec has unknown fields")
            run_spec = AcceptanceRunSpec(
                full_rc_sha=_string(spec_raw, "full_rc_sha"),
                dataset_id=_string(spec_raw, "dataset_id"),
                dataset_digest=_string(spec_raw, "dataset_digest"),
                strategy_id=_string(spec_raw, "strategy_id"),
                strategy_digest=_string(spec_raw, "strategy_digest"),
                config_digest=_string(spec_raw, "config_digest"),
                enable_soak_24h=_boolean(spec_raw.get("enable_soak_24h", False)),
                enable_soak_72h=_boolean(spec_raw.get("enable_soak_72h", False)),
                mode=_string_default(spec_raw.get("mode"), "PAPER"),
            )
            return AcceptanceMutationRequest(
                request_id=_string(payload, "request_id"),
                operator=self._operator_subject,
                mutation=mutation,
                spec=run_spec,
            )
        if set(payload) != {"request_id", "acceptance_run_id"}:
            raise ValueError("mutation body is invalid")
        return AcceptanceMutationRequest(
            request_id=_string(payload, "request_id"),
            operator=self._operator_subject,
            mutation=mutation,
            run_id=_string(payload, "acceptance_run_id"),
        )

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


def _snapshot(snapshot: AcceptanceRunSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "acceptance_run_id": snapshot.acceptance_run_id,
        "status": snapshot.status.value,
        "phase": snapshot.phase.value,
        "created_at": snapshot.created_at.isoformat(),
        "started_at": snapshot.started_at.isoformat() if snapshot.started_at is not None else None,
        "updated_at": snapshot.updated_at.isoformat(),
        "completed_at": (
            snapshot.completed_at.isoformat() if snapshot.completed_at is not None else None
        ),
        "active_test_time": snapshot.active_test_time,
        "wall_clock_elapsed": snapshot.wall_clock_elapsed,
        "restart_count": snapshot.restart_count,
        "checkpoint": (
            {
                "phase": snapshot.checkpoint.phase.value,
                "updated_at": snapshot.checkpoint.updated_at.isoformat(),
                "token": snapshot.checkpoint.token,
                "digest": snapshot.checkpoint.digest,
            }
            if snapshot.checkpoint is not None
            else None
        ),
        "resources": [
            {
                "timestamp": sample.timestamp.isoformat(),
                "cpu_percent": sample.cpu_percent,
                "ram_bytes": sample.ram_bytes,
                "disk_free_bytes": sample.disk_free_bytes,
            }
            for sample in snapshot.resources
        ],
        "warnings": list(snapshot.warnings),
        "error": snapshot.error,
        "next_action": snapshot.next_action,
        "evidence": [
            {"kind": item.kind, "digest": item.digest, "reference": item.reference}
            for item in snapshot.evidence
        ],
        "identity": {
            "full_rc_sha": snapshot.spec.full_rc_sha,
            "dataset_id": snapshot.spec.dataset_id,
            "dataset_digest": snapshot.spec.dataset_digest,
            "strategy_id": snapshot.spec.strategy_id,
            "strategy_digest": snapshot.spec.strategy_digest,
            "config_digest": snapshot.spec.config_digest,
            "mode": snapshot.spec.mode,
            "enable_soak_24h": snapshot.spec.enable_soak_24h,
            "enable_soak_72h": snapshot.spec.enable_soak_72h,
        },
        "phases": [phase.value for phase in snapshot.spec.phases],
    }


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")


def _string(mapping: dict[str, object], key: str) -> str:
    value = mapping[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value


def _string_default(value: object, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("value must be string")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("value must be boolean")
    return value


def _error_status(code: AcceptanceErrorCode) -> int:
    if code in {AcceptanceErrorCode.INVALID_REQUEST, AcceptanceErrorCode.PAPER_ONLY}:
        return 400
    if code in {AcceptanceErrorCode.IDEMPOTENCY_CONFLICT, AcceptanceErrorCode.RUN_ALREADY_ACTIVE}:
        return 409
    if code in {AcceptanceErrorCode.RUN_NOT_FOUND}:
        return 404
    if code in {
        AcceptanceErrorCode.RECOVERY_REQUIRED,
        AcceptanceErrorCode.STORAGE_FAILURE,
        AcceptanceErrorCode.OWNER_FAILURE,
        AcceptanceErrorCode.PHASE_FAILURE,
        AcceptanceErrorCode.PHASE_BLOCKED,
    }:
        return 503
    if code is AcceptanceErrorCode.INVALID_TRANSITION:
        return 422
    return 500
