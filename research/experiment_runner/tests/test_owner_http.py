from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from modular_research_runner.models import ExperimentStatus
from modular_research_runner.owner_http import (
    ResearchRunnerOwnerHttpClient,
    ResearchRunnerOwnerHttpServer,
)
from modular_research_runner.owner_identity import PreparedResearchIdentity
from modular_research_runner.repository import SqliteExperimentRepository
from modular_research_runner.runner import ResearchRunner

_EXPERIMENT_ID = "exp-" + "a" * 24
_OTHER_EXPERIMENT_ID = "exp-" + "c" * 24
_SPECIFICATION_SHA = "b" * 64
_STRATEGY_ID = "strategy-btc-paper"
_READER = "reader-token-0123456789"
_OPERATOR = "operator-token-0123456789"
_NOW = datetime(2026, 8, 20, tzinfo=UTC)


class _Scheduler:
    def __init__(self, repository: SqliteExperimentRepository, *, fail: bool = False) -> None:
        self.repository = repository
        self.fail = fail
        self.calls = 0

    def run(self, experiment_id: str, *, cancellation: object = None) -> object:
        del cancellation
        self.calls += 1
        if self.fail:
            raise RuntimeError("private owner failure /tmp/secret")
        self.repository.set_experiment_status(
            experiment_id,
            ExperimentStatus.COMPLETED,
            at=_NOW,
        )
        return SimpleNamespace(status=ExperimentStatus.COMPLETED)


def _runner(tmp_path, *, fail: bool = False):  # type: ignore[no-untyped-def]
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite")
    repository.register_experiment(
        experiment_id=_EXPERIMENT_ID,
        specification_sha256=_SPECIFICATION_SHA,
        manifest={
            "experiment_id": _EXPERIMENT_ID,
            "specification_sha256": _SPECIFICATION_SHA,
            "strategy_id": _STRATEGY_ID,
        },
        created_at=_NOW,
    )
    repository.set_experiment_status(_EXPERIMENT_ID, ExperimentStatus.PLANNED, at=_NOW)
    runner = object.__new__(ResearchRunner)
    runner.repository = repository
    scheduler = _Scheduler(repository, fail=fail)
    runner.scheduler = scheduler
    return runner, scheduler


def _decode(body: bytes) -> dict[str, object]:
    value = json.loads(body)
    assert isinstance(value, dict)
    return value


def test_owner_client_binds_one_existing_experiment_and_executes_same_runner(tmp_path) -> None:
    runner, scheduler = _runner(tmp_path)
    server = ResearchRunnerOwnerHttpServer(
        runner,
        experiment_id=_EXPERIMENT_ID,
        reader_token=_READER,
        operator_token=_OPERATOR,
        port=0,
    )
    server.start()
    host, port = server.address
    client = ResearchRunnerOwnerHttpClient(
        f"http://{host}:{port}",
        experiment_id=_EXPERIMENT_ID,
        reader_token=_READER,
        operator_token=_OPERATOR,
        execute_timeout_seconds=5,
    )
    try:
        identity = client.owner_identity()
        assert identity.experiment_id == _EXPERIMENT_ID
        assert identity.specification_sha256 == _SPECIFICATION_SHA
        assert identity.strategy_id == _STRATEGY_ID
        prepared = PreparedResearchIdentity.from_owner_identity(identity)
        assert prepared.manifest.experiment_id == _EXPERIMENT_ID
        assert prepared.specification.fingerprint == _SPECIFICATION_SHA
        assert prepared.specification.strategy_id == _STRATEGY_ID
        assert client.repository.experiment_status(_EXPERIMENT_ID) == "PLANNED"

        summary = client.execute(_EXPERIMENT_ID)
        assert summary.status == "COMPLETED"
        assert client.repository.experiment_status(_EXPERIMENT_ID) == "COMPLETED"
        assert scheduler.calls == 1
        assert _READER not in repr(client)
        assert _OPERATOR not in repr(client)
        assert _READER not in repr(server)
        assert _OPERATOR not in repr(server)
    finally:
        server.close()


def test_http_roles_and_fixed_experiment_reject_cross_owner_control(tmp_path) -> None:
    runner, scheduler = _runner(tmp_path)
    server = ResearchRunnerOwnerHttpServer(
        runner,
        experiment_id=_EXPERIMENT_ID,
        reader_token=_READER,
        operator_token=_OPERATOR,
        port=0,
    )
    try:
        denied_execute, _, body = server.response(
            method="POST",
            path="/owner/v1/research/execute",
            authorization=f"Bearer {_READER}",
            body=b"{}",
        )
        assert denied_execute == 401
        assert _decode(body) == {"error": "UNAUTHORIZED"}
        denied_read, _, _ = server.response(
            method="GET",
            path="/owner/v1/research",
            authorization=f"Bearer {_OPERATOR}",
        )
        assert denied_read == 401
        malformed, _, malformed_body = server.response(
            method="POST",
            path="/owner/v1/research/execute",
            authorization=f"Bearer {_OPERATOR}",
            body=b'{"experiment_id":"exp-bbbbbbbbbbbbbbbbbbbbbbbb"}',
        )
        assert malformed == 400
        assert _decode(malformed_body) == {"error": "INVALID_REQUEST"}
        assert scheduler.calls == 0

        server.start()
        host, port = server.address
        client = ResearchRunnerOwnerHttpClient(
            f"http://{host}:{port}",
            experiment_id=_EXPERIMENT_ID,
            reader_token=_READER,
            operator_token=_OPERATOR,
        )
        with pytest.raises(ValueError, match="different experiment"):
            client.repository.experiment_status(_OTHER_EXPERIMENT_ID)
        with pytest.raises(ValueError, match="different experiment"):
            client.execute(_OTHER_EXPERIMENT_ID)
        assert scheduler.calls == 0
    finally:
        server.close()


def test_owner_identity_drift_fails_closed_before_execute(tmp_path) -> None:
    runner, scheduler = _runner(tmp_path)
    server = ResearchRunnerOwnerHttpServer(
        runner,
        experiment_id=_EXPERIMENT_ID,
        reader_token=_READER,
        operator_token=_OPERATOR,
        port=0,
    )
    try:
        with sqlite3.connect(runner.repository.path) as connection:
            connection.execute(
                "UPDATE experiments SET specification_sha256 = ? WHERE experiment_id = ?",
                ("d" * 64, _EXPERIMENT_ID),
            )
        code, _, body = server.response(
            method="POST",
            path="/owner/v1/research/execute",
            authorization=f"Bearer {_OPERATOR}",
            body=b"{}",
        )
        assert code == 409
        assert _decode(body) == {"error": "OWNER_IDENTITY_CHANGED"}
        assert scheduler.calls == 0
    finally:
        server.close()


def test_owner_failure_is_bounded_and_remote_bind_is_rejected(tmp_path) -> None:
    runner, scheduler = _runner(tmp_path, fail=True)
    server = ResearchRunnerOwnerHttpServer(
        runner,
        experiment_id=_EXPERIMENT_ID,
        reader_token=_READER,
        operator_token=_OPERATOR,
        port=0,
    )
    try:
        code, _, body = server.response(
            method="POST",
            path="/owner/v1/research/execute",
            authorization=f"Bearer {_OPERATOR}",
            body=b"{}",
        )
        assert code == 503
        assert _decode(body) == {"error": "OWNER_FAILURE"}
        assert b"secret" not in body
        assert b"/tmp" not in body
        assert scheduler.calls == 1
    finally:
        server.close()

    with pytest.raises(ValueError, match="loopback"):
        ResearchRunnerOwnerHttpServer(
            runner,
            experiment_id=_EXPERIMENT_ID,
            reader_token=_READER,
            operator_token=_OPERATOR,
            host="0.0.0.0",
            port=0,
        )
    with pytest.raises(ValueError, match="loopback"):
        ResearchRunnerOwnerHttpClient(
            "http://example.com:8782",
            experiment_id=_EXPERIMENT_ID,
            reader_token=_READER,
            operator_token=_OPERATOR,
        )
    with pytest.raises(ValueError, match="distinct"):
        ResearchRunnerOwnerHttpClient(
            "http://127.0.0.1:8782",
            experiment_id=_EXPERIMENT_ID,
            reader_token="same-token-012345",
            operator_token="same-token-012345",
        )
