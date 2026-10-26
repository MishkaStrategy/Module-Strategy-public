from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast

import pytest

import modular_control_plane.cli_entrypoint as cli_entrypoint
from modular_control_plane.client import ClientSettings, MutationTransport, Transport
from modular_control_plane.notifications import NotificationStatus
from modular_control_plane.notifications import (
    TestNotificationRequest as NotificationTestRequest,
)
from modular_control_plane.notifications import (
    TestNotificationResult as NotificationTestResult,
)
from modular_control_plane.notifications_client import NotificationsControlClient

NOW = datetime(2026, 8, 9, 10, 30, tzinfo=UTC)


def status_payload(
    status: Literal["ok", "degraded", "unavailable"] = "ok",
) -> NotificationStatus:
    return NotificationStatus(
        configured=True,
        reachable=True,
        notifications_enabled=True,
        status=status,
        cycles=12,
        signals_sent=5,
        last_success_at=NOW,
        last_error_type=None,
    )


def test_notifications_client_uses_reader_transport_and_safe_contract() -> None:
    seen: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, headers, timeout))
        return 200, status_payload().model_dump_json().encode()

    client = NotificationsControlClient(
        ClientSettings(reader_token="reader-secret", operator_token="operator-secret"),
        transport=cast(Transport, transport),
    )

    status = client.notifications_status()

    assert status.status == "ok"
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/notifications/status",
            {"Accept": "application/json", "Authorization": "Bearer reader-secret"},
            2.0,
        )
    ]


def test_notifications_client_sends_only_request_id_with_operator_transport() -> None:
    seen: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def mutation_transport(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        seen.append((url, headers, json.loads(body), timeout))
        result = NotificationTestResult(
            request_id="terminal-test-1",
            operator="control-plane:local-operator",
            completed_at=NOW,
            replayed=False,
        )
        return 200, result.model_dump_json().encode()

    client = NotificationsControlClient(
        ClientSettings(reader_token="reader-secret", operator_token="operator-secret"),
        mutation_transport=cast(MutationTransport, mutation_transport),
    )

    result = client.notifications_test(NotificationTestRequest(request_id="terminal-test-1"))

    assert result.replayed is False
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/notifications/test",
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer operator-secret",
            },
            {"schema_version": 1, "request_id": "terminal-test-1"},
            2.0,
        )
    ]


def test_cli_entrypoint_delegates_existing_commands_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_entrypoint,
        "legacy_main",
        lambda argv: seen.append(list(argv)) or 7,
    )

    assert cli_entrypoint.main(["status", "--json"]) == 7
    assert seen == [["status", "--json"]]


def test_cli_entrypoint_does_not_intercept_notifications_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_entrypoint,
        "legacy_main",
        lambda argv: seen.append(list(argv)) or 8,
    )
    arguments = ["component", "notifications", "--json"]

    assert cli_entrypoint.main(arguments) == 8
    assert seen == [arguments]


def test_cli_entrypoint_detects_notifications_after_global_options() -> None:
    assert (
        cli_entrypoint._command(  # noqa: SLF001 - dispatcher regression
            ["--url", "http://127.0.0.1:8765", "--timeout=3", "notifications", "status"]
        )
        == "notifications"
    )


def test_cli_notifications_status_uses_same_reader_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: list[ClientSettings] = []

    class FakeClient:
        def __init__(self, settings: ClientSettings) -> None:
            captured.append(settings)

        def notifications_status(self) -> NotificationStatus:
            return status_payload("degraded")

    monkeypatch.setattr(cli_entrypoint, "NotificationsControlClient", FakeClient)
    monkeypatch.setenv("MODULAR_CONTROL_READER_TOKEN", "reader-secret")

    exit_code = cli_entrypoint.main(["notifications", "status", "--json"])

    assert exit_code == 2
    assert captured[0].reader_token == "reader-secret"
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_cli_notifications_test_requires_request_id_and_preserves_owner_replay(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[NotificationTestRequest] = []

    class FakeClient:
        def __init__(self, _settings: ClientSettings) -> None:
            pass

        def notifications_test(self, request: NotificationTestRequest) -> NotificationTestResult:
            requests.append(request)
            return NotificationTestResult(
                request_id=request.request_id,
                operator="control-plane:local-operator",
                completed_at=NOW,
                replayed=True,
            )

    monkeypatch.setattr(cli_entrypoint, "NotificationsControlClient", FakeClient)
    monkeypatch.setenv("MODULAR_CONTROL_OPERATOR_TOKEN", "operator-secret")

    exit_code = cli_entrypoint.main(
        ["notifications", "test", "--request-id", "terminal-replay-1", "--json"]
    )

    assert exit_code == 0
    assert [request.request_id for request in requests] == ["terminal-replay-1"]
    output = json.loads(capsys.readouterr().out)
    assert output["request_id"] == "terminal-replay-1"
    assert output["replayed"] is True
