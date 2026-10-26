from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from signal_bot.operator_control import SignalBotControlLedger, SignalBotOperatorControl
from signal_bot.operator_http import SignalBotControlHttpServer

NOW = datetime(2026, 8, 8, 16, 30, tzinfo=UTC)
READER = "reader-secret"
OPERATOR = "operator-secret"


class Sender:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def send_test_notification(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def build_server(
    path: Path,
    *,
    sender: Sender | None = None,
) -> tuple[SignalBotControlHttpServer, SignalBotControlLedger, Sender]:
    current_sender = sender or Sender()
    ledger = SignalBotControlLedger(path)
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {
            "status": "ok",
            "cycles": 9,
            "signals_sent": 2,
            "last_success_at": NOW,
            "token": "TOPSECRET",
            "chat_id": "12345",
        },
        notifications_enabled=lambda: True,
        sender=current_sender,
        configured=True,
        clock=lambda: NOW,
    )
    server = SignalBotControlHttpServer(
        control,
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
    )
    return server, ledger, current_sender


def decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def test_control_transport_rejects_remote_bind_and_equal_tokens(tmp_path: Path) -> None:
    server, ledger, _ = build_server(tmp_path / "base.db")
    server.close()
    ledger.close()

    ledger = SignalBotControlLedger(tmp_path / "reject.db")
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {"status": "ok"},
        notifications_enabled=lambda: True,
        sender=Sender(),
        configured=True,
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="loopback"):
        SignalBotControlHttpServer(
            control,
            reader_token=READER,
            operator_token=OPERATOR,
            host="0.0.0.0",
            port=0,
        )
    with pytest.raises(ValueError, match="distinct"):
        SignalBotControlHttpServer(
            control,
            reader_token="same-token",
            operator_token="same-token",
            port=0,
        )
    ledger.close()


def test_reader_status_is_safe_and_reader_token_cannot_mutate(tmp_path: Path) -> None:
    server, ledger, sender = build_server(tmp_path / "signal.db")

    status_code, _, body = server.response(
        method="GET",
        path="/control/v1/status",
        authorization=f"Bearer {READER}",
    )
    status = decode(body)
    assert status_code == 200
    assert status == {
        "configured": True,
        "cycles": 9,
        "last_error_type": None,
        "last_success_at": NOW.isoformat(),
        "notifications_enabled": True,
        "reachable": None,
        "signals_sent": 2,
        "status": "ok",
    }
    assert "TOPSECRET" not in body.decode()
    assert "12345" not in body.decode()

    mutation_code, _, mutation_body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {READER}",
        body=b'{"request_id":"reader-attempt"}',
    )
    assert mutation_code == 401
    assert decode(mutation_body) == {"error": "UNAUTHORIZED"}
    assert sender.calls == 0
    server.close()
    ledger.close()


def test_operator_token_mutates_and_exact_duplicate_replays_once(tmp_path: Path) -> None:
    server, ledger, sender = build_server(tmp_path / "signal.db")
    body = b'{"request_id":"operator-send"}'

    first_code, _, first_body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {OPERATOR}",
        body=body,
    )
    replay_code, _, replay_body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {OPERATOR}",
        body=body,
    )

    first = decode(first_body)
    replay = decode(replay_body)
    assert first_code == replay_code == 200
    assert first["request_id"] == replay["request_id"] == "operator-send"
    assert first["operator"] == replay["operator"] == "control-plane:local-operator"
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert sender.calls == 1
    server.close()
    ledger.close()


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"request_id":"x","message":"browser supplied"}',
        b'{"request_id":"x","operator":"browser"}',
        b'{"request_id":1}',
        b"not-json",
    ],
)
def test_operator_transport_rejects_arbitrary_payload_fields(tmp_path: Path, body: bytes) -> None:
    server, ledger, sender = build_server(tmp_path / "signal.db")

    code, _, response_body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {OPERATOR}",
        body=body,
    )

    assert code == 400
    assert decode(response_body) == {"error": "INVALID_REQUEST"}
    assert sender.calls == 0
    server.close()
    ledger.close()


def test_transport_bounds_body_and_does_not_echo_tokens(tmp_path: Path) -> None:
    server, ledger, sender = build_server(tmp_path / "signal.db")

    code, _, body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {OPERATOR}",
        body=b"x" * 4097,
    )

    assert code == 413
    assert decode(body) == {"error": "REQUEST_TOO_LARGE"}
    assert sender.calls == 0
    representation = repr(server)
    assert READER not in representation
    assert OPERATOR not in representation
    server.close()
    ledger.close()


def test_rate_limit_error_mapping_never_echoes_telegram_url(tmp_path: Path) -> None:
    sender = Sender(
        urllib.error.HTTPError(
            "https://api.telegram.org/botTOPSECRET/sendMessage",
            429,
            "rate limited",
            None,
            None,
        )
    )
    server, ledger, _ = build_server(tmp_path / "signal.db", sender=sender)

    code, _, body = server.response(
        method="POST",
        path="/control/v1/test-notification",
        authorization=f"Bearer {OPERATOR}",
        body=b'{"request_id":"rate-limit"}',
    )

    payload = decode(body)
    assert code == 429
    assert payload == {"error": "RATE_LIMITED", "request_id": "rate-limit"}
    text = body.decode().lower()
    assert "topsecret" not in text
    assert "api.telegram.org" not in text
    server.close()
    ledger.close()


def test_real_loopback_transport_preserves_reader_operator_boundary(tmp_path: Path) -> None:
    server, ledger, sender = build_server(tmp_path / "signal.db")
    server.start()
    host, port = server.address
    base = f"http://{host}:{port}"
    try:
        status_request = urllib.request.Request(
            f"{base}/control/v1/status",
            headers={"Authorization": f"Bearer {READER}"},
        )
        with urllib.request.urlopen(status_request, timeout=2) as response:
            assert response.status == 200
            assert decode(response.read())["configured"] is True

        reader_mutation = urllib.request.Request(
            f"{base}/control/v1/test-notification",
            data=b'{"request_id":"reader-loopback"}',
            headers={
                "Authorization": f"Bearer {READER}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(reader_mutation, timeout=2)
        assert denied.value.code == 401
        assert sender.calls == 0

        operator_mutation = urllib.request.Request(
            f"{base}/control/v1/test-notification",
            data=b'{"request_id":"operator-loopback"}',
            headers={
                "Authorization": f"Bearer {OPERATOR}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(operator_mutation, timeout=2) as response:
            assert response.status == 200
            assert decode(response.read())["replayed"] is False
        assert sender.calls == 1
    finally:
        server.close()
        ledger.close()
