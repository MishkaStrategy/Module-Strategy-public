from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

import modular_control_plane.cli_entrypoint as cli_entrypoint
from modular_control_plane.client import ClientSettings, Transport
from modular_control_plane.operational_events import (
    OperationalEvent,
    OperationalEventPage,
    OperationalEventSeverity,
)
from modular_control_plane.operational_events_client import OperationalEventsControlClient

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
CURSOR = "trr-v1:0123456789abcdef:3"
NEXT_CURSOR = "trr-v1:0123456789abcdef:7"


def event() -> OperationalEvent:
    return OperationalEvent(
        event_id="trr-000000000007-0123456789abcdef",
        source="trading_runner",
        resource="paper-runtime-1",
        timestamp=NOW,
        event_type="operator.lifecycle",
        severity=OperationalEventSeverity.INFO,
        state="running",
        payload={"operation": "START", "result": "SUCCEEDED"},
        cursor=NEXT_CURSOR,
    )


def page() -> OperationalEventPage:
    return OperationalEventPage(
        source="trading_runner",
        events=(event(),),
        next_cursor=NEXT_CURSOR,
        has_more=False,
        scanned_count=1,
        omitted_count=0,
        retention_limit=1000,
        truncated=False,
    )


def test_client_encodes_only_bounded_owner_query_parameters() -> None:
    seen: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, headers, timeout))
        return 200, page().model_dump_json().encode()

    client = OperationalEventsControlClient(
        ClientSettings(reader_token="reader-secret"),
        transport=cast(Transport, transport),
    )
    result = client.operational_events(
        cursor=CURSOR,
        limit=25,
        event_types=("operator.lifecycle", "runtime.failed"),
        severities=(OperationalEventSeverity.INFO, OperationalEventSeverity.ERROR),
    )

    assert result.next_cursor == NEXT_CURSOR
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/events/trading-runner?"
            "limit=25&cursor=trr-v1%3A0123456789abcdef%3A3&"
            "event_type=operator.lifecycle&event_type=runtime.failed&severity=INFO&severity=ERROR",
            {"Accept": "application/json", "Authorization": "Bearer reader-secret"},
            2.0,
        )
    ]


def test_client_rejects_paths_commands_and_unsupported_filters_before_transport() -> None:
    calls = 0

    def transport(_url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        nonlocal calls
        calls += 1
        return 200, page().model_dump_json().encode()

    client = OperationalEventsControlClient(
        ClientSettings(reader_token="reader-secret"),
        transport=cast(Transport, transport),
    )
    for invoke in (
        lambda: client.operational_events(cursor="/Users/private/runtime.log"),
        lambda: client.operational_events(limit=0),
        lambda: client.operational_events(event_types=("runtime.started", "runtime.started")),
        lambda: client.operational_events(event_types=("shell.command",)),
    ):
        with pytest.raises((TypeError, ValueError)):
            invoke()
    assert calls == 0


def test_cli_entrypoint_does_not_intercept_events_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_entrypoint,
        "legacy_main",
        lambda argv: seen.append(list(argv)) or 8,
    )
    arguments = ["component", "events", "--json"]

    assert cli_entrypoint.main(arguments) == 8
    assert seen == [arguments]


def test_cli_events_preserves_cursor_filters_and_json_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, settings: ClientSettings) -> None:
            assert settings.reader_token == "reader-secret"

        def operational_events(
            self,
            *,
            cursor: str | None = None,
            limit: int = 100,
            event_types: tuple[str, ...] = (),
            severities: tuple[OperationalEventSeverity, ...] = (),
        ) -> OperationalEventPage:
            calls.append(
                {
                    "cursor": cursor,
                    "limit": limit,
                    "event_types": event_types,
                    "severities": severities,
                }
            )
            return page()

    monkeypatch.setattr(cli_entrypoint, "OperationalEventsControlClient", FakeClient)
    monkeypatch.setenv("MODULAR_CONTROL_READER_TOKEN", "reader-secret")

    exit_code = cli_entrypoint.main(
        [
            "events",
            "--cursor",
            CURSOR,
            "--limit",
            "25",
            "--event-type",
            "operator.lifecycle",
            "--severity",
            "INFO",
            "--json",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "cursor": CURSOR,
            "limit": 25,
            "event_types": ("operator.lifecycle",),
            "severities": (OperationalEventSeverity.INFO,),
        }
    ]
    output = json.loads(capsys.readouterr().out)
    assert output["source"] == "trading_runner"
    assert output["next_cursor"] == NEXT_CURSOR
    assert output["events"][0]["event_type"] == "operator.lifecycle"
