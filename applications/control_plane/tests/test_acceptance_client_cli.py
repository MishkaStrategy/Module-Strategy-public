from __future__ import annotations

import json

import pytest

import modular_control_plane.cli as cli_module
from modular_control_plane.acceptance import (
    AcceptanceCurrent,
    AcceptanceMutation,
    AcceptanceMutationResult,
    AcceptancePhase,
    AcceptanceRunMutationRequest,
    AcceptanceStartRequest,
    AcceptanceStatus,
)
from modular_control_plane.client import ClientSettings, ControlClient

READER = "reader-secret"
OPERATOR = "operator-secret"
DATASET = "b" * 64
STRATEGY = "c" * 64
CONFIG = "d" * 64


def mutation_payload(mutation: str = "START", request_id: str = "request-1") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "request_id": request_id,
            "operator": "control-plane:local-operator",
            "mutation": mutation,
            "acceptance_run_id": "acceptance-v1",
            "status": "RUNNING",
            "phase": "SOAK_2H",
            "completed_at": "2026-08-09T07:00:00+00:00",
            "replayed": False,
        }
    ).encode()


def test_acceptance_client_keeps_existing_get_transport_signature() -> None:
    observed: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        observed.append((url, headers, timeout))
        return 200, b'{"schema_version":1,"run":null}'

    client = ControlClient(
        ClientSettings(reader_token=READER, operator_token=OPERATOR),
        transport=transport,
    )

    result = client.acceptance_current()

    assert result.run is None
    assert len(observed) == 1
    assert observed[0][1]["Authorization"] == f"Bearer {READER}"
    assert OPERATOR not in repr(client)
    assert READER not in repr(client)


def test_acceptance_client_posts_with_operator_credential_and_versioned_contract() -> None:
    observed: list[tuple[str, dict[str, str], bytes, float]] = []

    def mutation_transport(
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        observed.append((url, headers, body, timeout))
        return 200, mutation_payload()

    client = ControlClient(
        ClientSettings(reader_token=READER, operator_token=OPERATOR),
        mutation_transport=mutation_transport,
    )
    request = AcceptanceStartRequest(
        request_id="request-1",
        dataset_id="dataset-v1",
        dataset_digest=DATASET,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY,
        config_digest=CONFIG,
    )

    result = client.acceptance_start(request)

    assert result.mutation is AcceptanceMutation.START
    url, headers, body, _ = observed[0]
    assert url.endswith("/api/control/v1/acceptance/start")
    assert headers["Authorization"] == f"Bearer {OPERATOR}"
    assert READER not in headers["Authorization"]
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    assert payload["request_id"] == "request-1"
    assert "full_rc_sha" not in payload
    assert "mode" not in payload
    assert "operator" not in payload


def test_acceptance_client_preserves_run_mutation_identity() -> None:
    observed: list[dict[str, object]] = []

    def mutation_transport(
        _url: str,
        _headers: dict[str, str],
        body: bytes,
        _timeout: float,
    ) -> tuple[int, bytes]:
        observed.append(json.loads(body))
        return 200, mutation_payload("RETRY", "retry-1")

    client = ControlClient(
        ClientSettings(operator_token=OPERATOR),
        mutation_transport=mutation_transport,
    )
    result = client.acceptance_retry(
        AcceptanceRunMutationRequest(
            request_id="retry-1",
            acceptance_run_id="acceptance-v1",
        )
    )

    assert result.mutation is AcceptanceMutation.RETRY
    assert observed == [
        {
            "acceptance_run_id": "acceptance-v1",
            "request_id": "retry-1",
            "schema_version": 1,
        }
    ]


class FakeCliClient:
    def __init__(self) -> None:
        self.start_requests: list[AcceptanceStartRequest] = []
        self.run_requests: list[tuple[str, AcceptanceRunMutationRequest]] = []

    def acceptance_current(self) -> AcceptanceCurrent:
        return AcceptanceCurrent(run=None)

    def acceptance_start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        self.start_requests.append(request)
        return receipt(AcceptanceMutation.START, request.request_id)

    def acceptance_stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.run_requests.append(("stop", request))
        return receipt(AcceptanceMutation.STOP, request.request_id)

    def acceptance_cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.run_requests.append(("cancel", request))
        return receipt(AcceptanceMutation.CANCEL, request.request_id)

    def acceptance_retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.run_requests.append(("retry", request))
        return receipt(AcceptanceMutation.RETRY, request.request_id)


def receipt(mutation: AcceptanceMutation, request_id: str) -> AcceptanceMutationResult:
    return AcceptanceMutationResult(
        request_id=request_id,
        operator="control-plane:local-operator",
        mutation=mutation,
        acceptance_run_id="acceptance-v1",
        status=AcceptanceStatus.RUNNING,
        phase=AcceptancePhase.SOAK_2H,
        completed_at="2026-08-09T07:00:00+00:00",
        replayed=False,
    )


def install_fake_cli_client(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeCliClient,
) -> list[ClientSettings]:
    settings: list[ClientSettings] = []

    def factory(current: ClientSettings) -> FakeCliClient:
        settings.append(current)
        return fake

    monkeypatch.setattr(cli_module, "ControlClient", factory)
    return settings


def test_cli_acceptance_status_uses_reader_surface(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeCliClient()
    install_fake_cli_client(monkeypatch, fake)

    assert cli_module.main(["acceptance", "status"]) == 0
    assert "no durable run" in capsys.readouterr().out


def test_cli_acceptance_start_has_no_full_rc_or_mode_argument(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeCliClient()
    settings = install_fake_cli_client(monkeypatch, fake)
    monkeypatch.setenv("MODULAR_CONTROL_OPERATOR_TOKEN", OPERATOR)

    result = cli_module.main(
        [
            "acceptance",
            "start",
            "--request-id",
            "request-1",
            "--dataset-id",
            "dataset-v1",
            "--dataset-digest",
            DATASET,
            "--strategy-id",
            "strategy-v1",
            "--strategy-digest",
            STRATEGY,
            "--config-digest",
            CONFIG,
            "--soak-24h",
        ]
    )

    assert result == 0
    assert len(fake.start_requests) == 1
    request = fake.start_requests[0]
    assert request.request_id == "request-1"
    assert request.enable_soak_24h is True
    assert request.enable_soak_72h is False
    assert settings[0].operator_token == OPERATOR
    output = capsys.readouterr().out
    assert OPERATOR not in output
    assert "FULL_RC" not in output


def test_cli_acceptance_retry_preserves_run_and_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCliClient()
    install_fake_cli_client(monkeypatch, fake)

    result = cli_module.main(
        [
            "acceptance",
            "retry",
            "acceptance-v1",
            "--request-id",
            "retry-1",
            "--json",
        ]
    )

    assert result == 0
    assert len(fake.run_requests) == 1
    kind, request = fake.run_requests[0]
    assert kind == "retry"
    assert request.request_id == "retry-1"
    assert request.acceptance_run_id == "acceptance-v1"
