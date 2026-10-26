from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from modular_research_runner.owner_identity import (
    PreparedResearchIdentity,
    read_experiment_owner_identity,
)
from modular_research_runner.repository import SqliteExperimentRepository, StateRepositoryError

_EXPERIMENT_ID = "exp-" + "a" * 24
_SPECIFICATION_SHA = "b" * 64
_STRATEGY_ID = "strategy-btc-paper"


def _register(
    repository: SqliteExperimentRepository, *, manifest: dict[str, object] | None = None
) -> None:
    repository.register_experiment(
        experiment_id=_EXPERIMENT_ID,
        specification_sha256=_SPECIFICATION_SHA,
        manifest=(
            manifest
            if manifest is not None
            else {
                "experiment_id": _EXPERIMENT_ID,
                "specification_sha256": _SPECIFICATION_SHA,
                "strategy_id": _STRATEGY_ID,
            }
        ),
        created_at=datetime(2026, 8, 19, tzinfo=UTC),
    )


def test_reads_authoritative_identity_without_mutating_repository(tmp_path) -> None:
    path = tmp_path / "research.sqlite"
    repository = SqliteExperimentRepository(path)
    _register(repository)
    before = path.read_bytes()

    identity = read_experiment_owner_identity(repository, _EXPERIMENT_ID)

    assert identity.experiment_id == _EXPERIMENT_ID
    assert identity.specification_sha256 == _SPECIFICATION_SHA
    assert identity.strategy_id == _STRATEGY_ID
    assert path.read_bytes() == before

    prepared = PreparedResearchIdentity.from_owner_identity(identity)
    assert prepared.manifest.experiment_id == _EXPERIMENT_ID
    assert prepared.specification.fingerprint == _SPECIFICATION_SHA
    assert prepared.specification.strategy_id == _STRATEGY_ID


def test_missing_experiment_fails_closed(tmp_path) -> None:
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite")

    with pytest.raises(StateRepositoryError, match="not registered"):
        read_experiment_owner_identity(repository, _EXPERIMENT_ID)


def test_manifest_identity_mismatch_fails_closed(tmp_path) -> None:
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite")
    _register(
        repository,
        manifest={
            "experiment_id": "exp-" + "c" * 24,
            "specification_sha256": _SPECIFICATION_SHA,
            "strategy_id": _STRATEGY_ID,
        },
    )

    with pytest.raises(StateRepositoryError, match="manifest identity mismatch"):
        read_experiment_owner_identity(repository, _EXPERIMENT_ID)


def test_corrupted_stored_specification_digest_fails_closed(tmp_path) -> None:
    path = tmp_path / "research.sqlite"
    repository = SqliteExperimentRepository(path)
    _register(repository)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE experiments SET specification_sha256 = ? WHERE experiment_id = ?",
            ("not-a-digest", _EXPERIMENT_ID),
        )

    with pytest.raises(StateRepositoryError, match="specification identity mismatch"):
        read_experiment_owner_identity(repository, _EXPERIMENT_ID)


def test_invalid_requested_experiment_id_is_rejected_before_query(tmp_path) -> None:
    repository = SqliteExperimentRepository(tmp_path / "research.sqlite")

    with pytest.raises(StateRepositoryError, match="experiment_id is invalid"):
        read_experiment_owner_identity(repository, "../../research.sqlite")
