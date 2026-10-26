from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from http.client import HTTPConnection
from urllib.parse import urlsplit

from ..acceptance import (
    AcceptanceCurrent,
    AcceptanceMutationResult,
    AcceptanceOwnerResponseError,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_RESPONSE_BYTES = 512 * 1024
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_OWNER_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "IDEMPOTENCY_CONFLICT",
        "RUN_ALREADY_ACTIVE",
        "RUN_NOT_FOUND",
        "INVALID_TRANSITION",
        "RECOVERY_REQUIRED",
        "STORAGE_FAILURE",
        "OWNER_FAILURE",
        "PHASE_FAILURE",
        "PHASE_BLOCKED",
        "CANCELLED",
        "PAPER_ONLY",
    }
)


class SystemAcceptanceHttpClient:
    """Consume the pinned System Acceptance owner transport over local loopback only."""

    def __init__(
        self,
        base_url: str,
        *,
        reader_token: str,
        operator_token: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._base_url = validate_system_acceptance_base_url(base_url)
        _token(reader_token, "reader_token")
        if operator_token is not None:
            _token(operator_token, "operator_token")
            if hmac.compare_digest(reader_token.encode(), operator_token.encode()):
                raise ValueError("reader_token and operator_token must be distinct")
        if not 0.05 <= timeout_seconds <= 30.0:
            raise ValueError("timeout_seconds must be between 0.05 and 30")
        self._reader_token = reader_token
        self._operator_token = operator_token
        self._timeout_seconds = timeout_seconds
        parsed = urlsplit(self._base_url)
        assert parsed.hostname is not None
        self._host = parsed.hostname
        self._port = parsed.port or 80

    @property
    def operator_available(self) -> bool:
        return self._operator_token is not None

    def current(self) -> AcceptanceCurrent:
        payload = self._request_json(
            "GET",
            "/acceptance/v1/current",
            token=self._reader_token,
        )
        try:
            return AcceptanceCurrent.model_validate(payload)
        except ValueError as exc:
            raise AcceptanceOwnerResponseError("OWNER_FAILURE") from exc

    def start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        return self._mutation(
            "/acceptance/v1/start",
            {
                "request_id": request.request_id,
                "spec": {
                    "dataset_id": request.dataset_id,
                    "dataset_digest": request.dataset_digest,
                    "strategy_id": request.strategy_id,
                    "strategy_digest": request.strategy_digest,
                    "config_digest": request.config_digest,
                    "enable_soak_24h": request.enable_soak_24h,
                    "enable_soak_72h": request.enable_soak_72h,
                },
            },
        )

    def stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._run_mutation("/acceptance/v1/stop", request)

    def cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._run_mutation("/acceptance/v1/cancel", request)

    def retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        return self._run_mutation("/acceptance/v1/retry", request)

    def _run_mutation(
        self,
        path: str,
        request: AcceptanceRunMutationRequest,
    ) -> AcceptanceMutationResult:
        return self._mutation(
            path,
            {
                "request_id": request.request_id,
                "acceptance_run_id": request.acceptance_run_id,
            },
        )

    def _mutation(self, path: str, body: dict[str, object]) -> AcceptanceMutationResult:
        token = self._operator_token
        if token is None:
            raise AcceptanceOwnerResponseError("OPERATOR_NOT_CONFIGURED")
        payload = self._request_json("POST", path, token=token, body=body)
        try:
            return AcceptanceMutationResult.model_validate(payload)
        except ValueError as exc:
            raise AcceptanceOwnerResponseError("OWNER_FAILURE") from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
    ) -> Mapping[str, object]:
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        if body is not None:
            encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection(self._host, self._port, timeout=self._timeout_seconds)
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "")
            cache_control = response.getheader("Cache-Control", "")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError) as exc:
            raise AcceptanceOwnerResponseError("OWNER_UNAVAILABLE") from exc
        finally:
            connection.close()
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AcceptanceOwnerResponseError("OWNER_FAILURE")
        if "application/json" not in content_type.lower():
            raise AcceptanceOwnerResponseError("OWNER_FAILURE")
        if cache_control.lower() != "no-store":
            raise AcceptanceOwnerResponseError("OWNER_FAILURE")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AcceptanceOwnerResponseError("OWNER_FAILURE") from exc
        if not isinstance(payload, Mapping):
            raise AcceptanceOwnerResponseError("OWNER_FAILURE")
        if status != 200:
            self._raise_owner_error(payload)
        return payload

    @staticmethod
    def _raise_owner_error(payload: Mapping[str, object]) -> None:
        raw_code = payload.get("error")
        code = (
            raw_code
            if isinstance(raw_code, str)
            and _ERROR_CODE.fullmatch(raw_code) is not None
            and raw_code in _OWNER_ERROR_CODES
            else "OWNER_FAILURE"
        )
        request_id = payload.get("request_id")
        run_id = payload.get("acceptance_run_id")
        raise AcceptanceOwnerResponseError(
            code,
            request_id=request_id if isinstance(request_id, str) else None,
            acceptance_run_id=run_id if isinstance(run_id, str) else None,
        )


def validate_system_acceptance_base_url(base_url: str) -> str:
    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "http":
        raise ValueError("System Acceptance URL must use HTTP on the local loopback boundary")
    if parsed.hostname is None or parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError("System Acceptance URL must target a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("System Acceptance URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("System Acceptance URL must be a pathless loopback base URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("System Acceptance URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("System Acceptance URL port must be between 1 and 65535")
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}{f':{port}' if port is not None else ''}"


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 4096:
        raise ValueError(f"{name} must contain 16..4096 characters")


__all__ = ["SystemAcceptanceHttpClient", "validate_system_acceptance_base_url"]
