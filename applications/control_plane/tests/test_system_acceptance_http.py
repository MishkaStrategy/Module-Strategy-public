from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from modular_control_plane.acceptance import (
    AcceptanceMutation,
    AcceptanceOwnerResponseError,
    AcceptancePhase,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
    AcceptanceStatus,
)
from modular_control_plane.adapters.system_acceptance_http import (
    SystemAcceptanceHttpClient,
    validate_system_acceptance_base_url,
)

READER_TOKEN = "reader-token-1234567890"
OPERATOR_TOKEN = "operator-token-1234567890"
FULL_RC = "a" * 40
DATASET = "b" * 64
STRATEGY = "c" * 64
CONFIG = "d" * 64
EVIDENCE = "e" * 64


def snapshot_payload() -> dict[str, object]:
    return {
        "acceptance_run_id": "acceptance-v1",
        "status": "RUNNING",
        "phase": "SOAK_2H",
        "created_at": "2026-08-09T06:30:00+00:00",
        "started_at": "2026-08-09T06:30:01+00:00",
        "updated_at": "2026-08-09T06:30:20+00:00",
        "completed_at": None,
        "active_test_time": 12.5,
        "wall_clock_elapsed": 20.0,
        "restart_count": 1,
        "checkpoint": {
            "phase": "SOAK_2H",
            "updated_at": "2026-08-09T06:30:15+00:00",
            "token": "soak:checkpoint:1",
            "digest": EVIDENCE,
        },
        "resources": [
            {
                "timestamp": "2026-08-09T06:30:10+00:00",
                "cpu_percent": 12.0,
                "ram_bytes": 1024,
                "disk_free_bytes": 4096,
            }
        ],
        "warnings": ["PROBE_DEGRADED"],
        "error": None,
        "next_action": "CONTINUE",
        "evidence": [
            {
                "kind": "acceptance.checkpoint",
                "digest": EVIDENCE,
                "reference": "checkpoint:abc123",
            }
        ],
        "identity": {
            "full_rc_sha": FULL_RC,
            "dataset_id": "dataset-v1",
            "dataset_digest": DATASET,
            "strategy_id": "strategy-v1",
            "strategy_digest": STRATEGY,
            "config_digest": CONFIG,
            "mode": "PAPER",
            "enable_soak_24h": True,
            "enable_soak_72h": False,
        },
        "phases": [
            "ENVIRONMENT",
            "DATASET",
            "BACKTEST_A",
            "BACKTEST_B",
            "DETERMINISM",
            "RESEARCH",
            "RUNTIME_SMOKE",
            "RECOVERY",
            "SOAK_2H",
            "SOAK_24H",
            "FINAL_VERIFICATION",
        ],
    }


def receipt_payload(mutation: str, request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "operator": "control-plane:local-operator",
        "mutation": mutation,
        "acceptance_run_id": "acceptance-v1",
        "status": "RUNNING",
        "phase": "SOAK_2H",
        "completed_at": "2026-08-09T06:31:00+00:00",
        "replayed": False,
    }


class LocalOwnerServer:
    def __init__(
        self,
        responder: Callable[[str, str, str, object | None], tuple[int, Mapping[str, object]]],
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.requests: list[tuple[str, str, str, object | None]] = []
        current = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                body: object | None = json.loads(raw) if raw else None
                authorization = self.headers.get("Authorization", "")
                current.requests.append((method, self.path, authorization, body))
                status, payload = responder(method, self.path, authorization, body)
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if cache_control:
                    self.send_header("Cache-Control", cache_control)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    def __enter__(self) -> LocalOwnerServer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def normal_responder(
    method: str,
    path: str,
    authorization: str,
    body: object | None,
) -> tuple[int, Mapping[str, object]]:
    if method == "GET" and path == "/acceptance/v1/current":
        assert authorization == f"Bearer {READER_TOKEN}"
        return 200, {"run": snapshot_payload()}
    assert method == "POST"
    assert authorization == f"Bearer {OPERATOR_TOKEN}"
    assert isinstance(body, dict)
    mutation = {
        "/acceptance/v1/start": "START",
        "/acceptance/v1/stop": "STOP",
        "/acceptance/v1/cancel": "CANCEL",
        "/acceptance/v1/retry": "RETRY",
    }[path]
    request_id = body["request_id"]
    assert isinstance(request_id, str)
    return 200, receipt_payload(mutation, request_id)


def client(
    base_url: str, *, operator_token: str | None = OPERATOR_TOKEN
) -> SystemAcceptanceHttpClient:
    return SystemAcceptanceHttpClient(
        base_url,
        reader_token=READER_TOKEN,
        operator_token=operator_token,
        timeout_seconds=1.0,
    )


def start_request() -> AcceptanceStartRequest:
    return AcceptanceStartRequest(
        request_id="acceptance-start-1",
        dataset_id="dataset-v1",
        dataset_digest=DATASET,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY,
        config_digest=CONFIG,
        enable_soak_24h=True,
        enable_soak_72h=False,
    )


def test_current_reads_complete_owner_projection_with_reader_credential() -> None:
    with LocalOwnerServer(normal_responder) as server:
        current = client(server.base_url).current()

        assert current.run is not None
        assert current.run.status is AcceptanceStatus.RUNNING
        assert current.run.phase is AcceptancePhase.SOAK_2H
        assert current.run.active_test_time == 12.5
        assert current.run.wall_clock_elapsed == 20.0
        assert current.run.identity.full_rc_sha == FULL_RC
        assert current.run.evidence[0].reference == "checkpoint:abc123"
        assert server.requests[0][2] == f"Bearer {READER_TOKEN}"


def test_start_forwards_only_owner_allowed_fields_and_never_browser_candidate_sha() -> None:
    with LocalOwnerServer(normal_responder) as server:
        result = client(server.base_url).start(start_request())

        assert result.mutation is AcceptanceMutation.START
        _, path, authorization, body = server.requests[-1]
        assert path == "/acceptance/v1/start"
        assert authorization == f"Bearer {OPERATOR_TOKEN}"
        assert body == {
            "request_id": "acceptance-start-1",
            "spec": {
                "config_digest": CONFIG,
                "dataset_digest": DATASET,
                "dataset_id": "dataset-v1",
                "enable_soak_24h": True,
                "enable_soak_72h": False,
                "strategy_digest": STRATEGY,
                "strategy_id": "strategy-v1",
            },
        }
        assert "full_rc_sha" not in json.dumps(body)
        assert "mode" not in json.dumps(body)
        assert "schema_version" not in json.dumps(body)
        assert "operator" not in json.dumps(body)


@pytest.mark.parametrize(
    ("method", "path", "mutation"),
    [
        ("stop", "/acceptance/v1/stop", AcceptanceMutation.STOP),
        ("cancel", "/acceptance/v1/cancel", AcceptanceMutation.CANCEL),
        ("retry", "/acceptance/v1/retry", AcceptanceMutation.RETRY),
    ],
)
def test_run_mutations_preserve_same_request_and_run_identity(
    method: str,
    path: str,
    mutation: AcceptanceMutation,
) -> None:
    request = AcceptanceRunMutationRequest(
        request_id="acceptance-action-1",
        acceptance_run_id="acceptance-v1",
    )
    with LocalOwnerServer(normal_responder) as server:
        result = getattr(client(server.base_url), method)(request)

        assert result.mutation is mutation
        assert server.requests[-1][1] == path
        assert server.requests[-1][3] == {
            "acceptance_run_id": "acceptance-v1",
            "request_id": "acceptance-action-1",
        }


def test_owner_error_exposes_only_bounded_taxonomy_not_raw_secret_text() -> None:
    def responder(
        _method: str,
        _path: str,
        _authorization: str,
        _body: object | None,
    ) -> tuple[int, Mapping[str, object]]:
        return 409, {
            "error": "IDEMPOTENCY_CONFLICT",
            "request_id": "request-1",
            "acceptance_run_id": "acceptance-v1",
            "message": "/Users/private token=TOPSECRET",
        }

    with (
        LocalOwnerServer(responder) as server,
        pytest.raises(AcceptanceOwnerResponseError) as raised,
    ):
        client(server.base_url).stop(
            AcceptanceRunMutationRequest(
                request_id="request-1",
                acceptance_run_id="acceptance-v1",
            )
        )

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"
    assert str(raised.value) == "IDEMPOTENCY_CONFLICT"
    assert "/Users/" not in str(raised.value)
    assert "TOPSECRET" not in str(raised.value)


def test_missing_operator_credential_fails_before_network_mutation() -> None:
    with LocalOwnerServer(normal_responder) as server:
        current = client(server.base_url, operator_token=None)
        with pytest.raises(AcceptanceOwnerResponseError) as raised:
            current.start(start_request())

        assert raised.value.code == "OPERATOR_NOT_CONFIGURED"
        assert server.requests == []


def test_owner_response_must_be_no_store() -> None:
    with (
        LocalOwnerServer(normal_responder, cache_control="public") as server,
        pytest.raises(AcceptanceOwnerResponseError) as raised,
    ):
        client(server.base_url).current()

    assert raised.value.code == "OWNER_FAILURE"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8781",
        "http://example.com:8781",
        "http://user:pass@127.0.0.1:8781",
        "http://127.0.0.1:8781/acceptance/v1/current",
        "http://127.0.0.1:8781?path=/tmp/log",
    ],
)
def test_base_url_rejects_non_loopback_credentials_paths_and_queries(url: str) -> None:
    with pytest.raises(ValueError):
        validate_system_acceptance_base_url(url)


def test_distinct_downstream_credentials_are_required() -> None:
    with pytest.raises(ValueError, match="distinct"):
        SystemAcceptanceHttpClient(
            "http://127.0.0.1:8781",
            reader_token=READER_TOKEN,
            operator_token=READER_TOKEN,
        )
