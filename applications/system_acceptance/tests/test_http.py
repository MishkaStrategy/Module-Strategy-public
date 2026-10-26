from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
    SQLiteAcceptanceStore,
    SystemAcceptanceHttpServer,
    SystemAcceptanceOwner,
)

NOW = datetime(2026, 8, 8, 20, 0, tzinfo=UTC)
READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


class Executor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


def build_server(path: Path) -> tuple[SystemAcceptanceHttpServer, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="http-owner",
        clock=lambda: NOW,
        lease_seconds=5,
    )
    server = SystemAcceptanceHttpServer(
        owner,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
    )
    return server, store


def decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def start_body(request_id: str = "http-start") -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "spec": {
                "full_rc_sha": "a" * 40,
                "dataset_id": "dataset-v1",
                "dataset_digest": "b" * 64,
                "strategy_id": "strategy-v1",
                "strategy_digest": "c" * 64,
                "config_digest": "d" * 64,
                "enable_soak_24h": True,
                "enable_soak_72h": False,
                "mode": "PAPER",
            },
        },
        separators=(",", ":"),
    ).encode()


def test_transport_is_loopback_only_and_tokens_are_distinct(tmp_path: Path) -> None:
    server, store = build_server(tmp_path / "base.db")
    server.close()
    store.close()

    store = SQLiteAcceptanceStore(tmp_path / "reject.db")
    executor = Executor()
    owner = SystemAcceptanceOwner(
        store,
        AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase}),
        instance_id="reject-owner",
        clock=lambda: NOW,
        lease_seconds=5,
    )
    with pytest.raises(ValueError, match="loopback"):
        SystemAcceptanceHttpServer(
            owner,
            reader_token=READER,
            operator_token=OPERATOR,
            host="0.0.0.0",
            port=0,
        )
    with pytest.raises(ValueError, match="distinct"):
        SystemAcceptanceHttpServer(
            owner,
            reader_token="same-token-012345",
            operator_token="same-token-012345",
            port=0,
        )
    store.close()


def test_reader_token_reads_but_cannot_mutate(tmp_path: Path) -> None:
    server, store = build_server(tmp_path / "acceptance.db")

    code, headers, body = server.response(
        method="GET",
        path="/acceptance/v1/current",
        authorization=f"Bearer {READER}",
    )
    assert code == 200
    assert headers["Cache-Control"] == "no-store"
    assert decode(body) == {"run": None}

    denied, _, denied_body = server.response(
        method="POST",
        path="/acceptance/v1/start",
        authorization=f"Bearer {READER}",
        body=start_body(),
    )
    assert denied == 401
    assert decode(denied_body) == {"error": "UNAUTHORIZED"}
    assert server._owner.snapshot() is None  # noqa: SLF001 - verifies auth boundary side effect
    server.close()
    store.close()


def test_operator_start_duplicate_and_safe_reader_projection(tmp_path: Path) -> None:
    server, store = build_server(tmp_path / "acceptance.db")
    body = start_body("operator-start")

    first_code, _, first_body = server.response(
        method="POST",
        path="/acceptance/v1/start",
        authorization=f"Bearer {OPERATOR}",
        body=body,
    )
    replay_code, _, replay_body = server.response(
        method="POST",
        path="/acceptance/v1/start",
        authorization=f"Bearer {OPERATOR}",
        body=body,
    )
    first = decode(first_body)
    replay = decode(replay_body)

    assert first_code == replay_code == 200
    assert first["acceptance_run_id"] == replay["acceptance_run_id"]
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert first["operator"] == "control-plane:local-operator"

    read_code, _, read_body = server.response(
        method="GET",
        path="/acceptance/v1/current",
        authorization=f"Bearer {READER}",
    )
    assert read_code == 200
    projection = decode(read_body)["run"]
    assert isinstance(projection, dict)
    assert projection["status"] == "QUEUED"
    assert projection["identity"]["mode"] == "PAPER"
    text = read_body.decode().lower()
    assert READER.lower() not in text
    assert OPERATOR.lower() not in text
    assert "/users/" not in text
    assert "token=" not in text
    server.close()
    store.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b'{"request_id":"x","command":"pytest"}',
        b'{"request_id":"x","path":"/tmp/private"}',
        b'{"request_id":"x","spec":{"full_rc_sha":"bad"}}',
        b"not-json",
    ],
)
def test_start_rejects_arbitrary_command_path_and_malformed_payloads(
    tmp_path: Path,
    payload: bytes,
) -> None:
    server, store = build_server(tmp_path / "acceptance.db")

    code, _, body = server.response(
        method="POST",
        path="/acceptance/v1/start",
        authorization=f"Bearer {OPERATOR}",
        body=payload,
    )

    assert code == 400
    assert decode(body)["error"] == "INVALID_REQUEST"
    assert server._owner.snapshot() is None  # noqa: SLF001
    server.close()
    store.close()


def test_real_loopback_http_preserves_reader_operator_boundary(tmp_path: Path) -> None:
    server, store = build_server(tmp_path / "acceptance.db")
    server.start()
    host, port = server.address
    base = f"http://{host}:{port}"
    try:
        reader = urllib.request.Request(
            f"{base}/acceptance/v1/current",
            headers={"Authorization": f"Bearer {READER}"},
        )
        with urllib.request.urlopen(reader, timeout=2) as response:
            assert response.status == 200
            assert decode(response.read()) == {"run": None}

        denied = urllib.request.Request(
            f"{base}/acceptance/v1/start",
            data=start_body("reader-denied"),
            headers={"Authorization": f"Bearer {READER}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(denied, timeout=2)
        assert raised.value.code == 401

        allowed = urllib.request.Request(
            f"{base}/acceptance/v1/start",
            data=start_body("operator-http"),
            headers={"Authorization": f"Bearer {OPERATOR}"},
            method="POST",
        )
        with urllib.request.urlopen(allowed, timeout=2) as response:
            assert response.status == 200
            payload = decode(response.read())
            assert payload["status"] == "QUEUED"
    finally:
        server.close()
        store.close()
