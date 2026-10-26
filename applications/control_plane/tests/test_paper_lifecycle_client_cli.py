from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import pytest

import modular_control_plane.cli_entrypoint as cli_entrypoint
from modular_control_plane.client import ClientSettings, MutationTransport
from modular_control_plane.paper_lifecycle import (
    PaperLifecycleMutation,
    PaperLifecycleMutationRequest,
    PaperLifecycleMutationResult,
)
from modular_control_plane.paper_lifecycle_client import PaperLifecycleControlClient

NOW = datetime(2026, 8, 9, 14, 30, tzinfo=UTC)


def result(
    mutation: PaperLifecycleMutation,
    *,
    request_id: str = "terminal-lifecycle-1",
    replayed: bool = False,
) -> PaperLifecycleMutationResult:
    states = {
        PaperLifecycleMutation.START: ("configured", "running"),
        PaperLifecycleMutation.STOP: ("running", "stopped"),
        PaperLifecycleMutation.ACTIVATE_KILL_SWITCH: ("running", "kill_switch_active"),
        PaperLifecycleMutation.RESET_KILL_SWITCH: ("kill_switch_active", "stopped"),
    }
    previous_state, new_state = states[mutation]
    return PaperLifecycleMutationResult(
        request_id=request_id,
        operator="control-plane:local-operator",
        mutation=mutation,
        previous_state=previous_state,
        new_state=new_state,
        config_hash="a" * 64,
        completed_at=NOW,
        replayed=replayed,
    )


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (PaperLifecycleMutation.START, "/api/control/v1/paper/start"),
        (PaperLifecycleMutation.STOP, "/api/control/v1/paper/stop"),
        (
            PaperLifecycleMutation.ACTIVATE_KILL_SWITCH,
            "/api/control/v1/paper/kill-switch/activate",
        ),
        (
            PaperLifecycleMutation.RESET_KILL_SWITCH,
            "/api/control/v1/paper/kill-switch/reset",
        ),
    ],
)
def test_lifecycle_client_sends_only_request_id_with_operator_transport(
    mutation: PaperLifecycleMutation,
    path: str,
) -> None:
    seen: list[tuple[str, dict[str, str], dict[str, object], float]] = []

    def mutation_transport(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        seen.append((url, headers, json.loads(body), timeout))
        return 200, result(mutation).model_dump_json().encode()

    client = PaperLifecycleControlClient(
        ClientSettings(reader_token="reader-secret", operator_token="operator-secret"),
        mutation_transport=cast(MutationTransport, mutation_transport),
    )

    receipt = client.paper_lifecycle_mutate(
        mutation,
        PaperLifecycleMutationRequest(request_id="terminal-lifecycle-1"),
    )

    assert receipt.mutation is mutation
    assert seen == [
        (
            f"http://127.0.0.1:8765{path}",
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer operator-secret",
            },
            {"schema_version": 1, "request_id": "terminal-lifecycle-1"},
            2.0,
        )
    ]


def test_lifecycle_client_rejects_response_identity_mismatch() -> None:
    def mutation_transport(
        _url: str,
        _headers: dict[str, str],
        _body: bytes,
        _timeout: float,
    ) -> tuple[int, bytes]:
        return 200, result(
            PaperLifecycleMutation.STOP,
            request_id="different-request",
        ).model_dump_json().encode()

    client = PaperLifecycleControlClient(
        ClientSettings(operator_token="operator-secret"),
        mutation_transport=cast(MutationTransport, mutation_transport),
    )

    with pytest.raises(Exception) as raised:
        client.paper_lifecycle_mutate(
            PaperLifecycleMutation.START,
            PaperLifecycleMutationRequest(request_id="expected-request"),
        )

    assert getattr(raised.value, "code", None) == "CONTROL_RESPONSE_INVALID"


def test_cli_entrypoint_does_not_intercept_paper_lifecycle_as_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli_entrypoint,
        "legacy_main",
        lambda argv: seen.append(list(argv)) or 8,
    )
    arguments = ["component", "paper-lifecycle", "--json"]

    assert cli_entrypoint.main(arguments) == 8
    assert seen == [arguments]


def test_cli_entrypoint_detects_paper_lifecycle_after_global_options() -> None:
    assert (
        cli_entrypoint._command(  # noqa: SLF001 - dispatcher regression
            ["--url", "http://127.0.0.1:8765", "--timeout=3", "paper-lifecycle", "start"]
        )
        == "paper-lifecycle"
    )


@pytest.mark.parametrize(
    ("command", "mutation"),
    [
        ("start", PaperLifecycleMutation.START),
        ("stop", PaperLifecycleMutation.STOP),
        ("kill", PaperLifecycleMutation.ACTIVATE_KILL_SWITCH),
        ("reset", PaperLifecycleMutation.RESET_KILL_SWITCH),
    ],
)
def test_cli_paper_lifecycle_preserves_owner_request_id_and_replay(
    command: str,
    mutation: PaperLifecycleMutation,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[tuple[PaperLifecycleMutation, PaperLifecycleMutationRequest]] = []

    class FakeClient:
        def __init__(self, settings: ClientSettings) -> None:
            assert settings.operator_token == "operator-secret"

        def paper_lifecycle_mutate(
            self,
            operation: PaperLifecycleMutation,
            request: PaperLifecycleMutationRequest,
        ) -> PaperLifecycleMutationResult:
            requests.append((operation, request))
            return result(operation, request_id=request.request_id, replayed=True)

    monkeypatch.setattr(cli_entrypoint, "PaperLifecycleControlClient", FakeClient)
    monkeypatch.setenv("MODULAR_CONTROL_OPERATOR_TOKEN", "operator-secret")

    exit_code = cli_entrypoint.main(
        ["paper-lifecycle", command, "--request-id", "terminal-replay-1", "--json"]
    )

    assert exit_code == 0
    assert [(item[0], item[1].request_id) for item in requests] == [(mutation, "terminal-replay-1")]
    output = json.loads(capsys.readouterr().out)
    assert output["request_id"] == "terminal-replay-1"
    assert output["mutation"] == mutation.value
    assert output["replayed"] is True
