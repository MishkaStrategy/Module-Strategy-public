from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from modular_research_runner.artifacts import AtomicJsonArtifactStore
from modular_research_runner.canonical import canonical_json_loads, canonical_value
from modular_research_runner.engine import ModularBacktestEngineAdapter
from modular_research_runner.manifest import GitProvenance
from modular_research_runner.owner_identity import (
    PreparedResearchIdentity,
    ResearchExperimentOwnerIdentity,
    read_experiment_owner_identity,
)
from modular_research_runner.planning import ResearchPlanBuilder
from modular_research_runner.policies import ResumePolicy, RetryPolicy
from modular_research_runner.repository import SqliteExperimentRepository, StateRepositoryError
from modular_research_runner.restart import PreparedExperimentLoader, _decode_manifest
from modular_research_runner.runner import ResearchRunner
from modular_research_runner.scheduler import LocalResearchScheduler
from modular_research_runner.specification import SpecificationError


def _runner(root: Path, specification: object) -> ResearchRunner:
    repository = SqliteExperimentRepository(root / "research.sqlite3")
    retry = RetryPolicy.from_mapping(specification.retry_policy)
    resume = ResumePolicy.from_mapping(specification.resume_policy)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(
            lambda job: job,
            lambda job: SimpleNamespace(
                run_id=job.run_id,
                output_directory=job.output_directory,
                bars_processed=0,
                restart_verified=True,
            ),
        ),
        retry_policy=retry,
        resume_policy=resume,
        maximum_workers=min(
            specification.parallelism, specification.resource_limits["maximum_workers"]
        ),
    )
    return ResearchRunner(
        repository=repository,
        artifact_store=AtomicJsonArtifactStore(root / "artifacts"),
        plan_builder=ResearchPlanBuilder(root / "outputs"),
        scheduler=scheduler,
        backtest_engine_version="engine-v1",
        metric_schema_version="metrics-v1",
        module_api_version="module-api-v1",
    )


def _prepare(root: Path, specification: object) -> tuple[ResearchRunner, object]:
    runner = _runner(root, specification)
    prepared = runner.prepare(
        specification,
        git=GitProvenance(branch="stage1", commit_sha="a" * 40, clean=True),
    )
    return runner, prepared


def test_reconstructs_same_prepared_experiment_without_prepare(
    tmp_path: Path, specification: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, original = _prepare(tmp_path, specification)
    restarted = _runner(tmp_path, specification)
    monkeypatch.setattr(
        restarted,
        "prepare",
        lambda *args, **kwargs: pytest.fail("prepare() must not run during reconstruction"),
    )

    loaded = PreparedExperimentLoader(
        restarted,
        experiment_id=original.manifest.experiment_id,
        specification_sha256=original.specification.fingerprint,
        strategy_id=original.specification.strategy_id,
    ).load()

    assert canonical_value(loaded) == canonical_value(original)
    assert loaded.manifest.experiment_id == original.manifest.experiment_id
    assert loaded.specification.fingerprint == original.specification.fingerprint


def test_research_configured_identity_mismatch_fails_closed(
    tmp_path: Path, specification: object
) -> None:
    _, original = _prepare(tmp_path, specification)
    restarted = _runner(tmp_path, specification)

    with pytest.raises(SpecificationError, match="strategy identity mismatch"):
        PreparedExperimentLoader(
            restarted,
            experiment_id=original.manifest.experiment_id,
            specification_sha256=original.specification.fingerprint,
            strategy_id="another-strategy",
        ).load()


def test_research_corrupt_artifact_fails_closed(tmp_path: Path, specification: object) -> None:
    _, original = _prepare(tmp_path, specification)
    specification_path = (
        tmp_path
        / "artifacts"
        / "experiments"
        / original.manifest.experiment_id
        / "specification.json"
    )
    specification_path.write_text("{}", encoding="utf-8")

    with pytest.raises(SpecificationError):
        PreparedExperimentLoader(
            _runner(tmp_path, specification),
            experiment_id=original.manifest.experiment_id,
            specification_sha256=original.specification.fingerprint,
            strategy_id=original.specification.strategy_id,
        ).load()


def test_research_repository_manifest_mismatch_fails_closed(
    tmp_path: Path, specification: object
) -> None:
    runner, original = _prepare(tmp_path, specification)
    with sqlite3.connect(runner.repository.path) as connection:
        row = connection.execute(
            "SELECT manifest_json FROM experiments WHERE experiment_id = ?",
            (original.manifest.experiment_id,),
        ).fetchone()
        assert row is not None
        manifest = json.loads(row[0])
        manifest["strategy_id"] = "another-strategy"
        connection.execute(
            "UPDATE experiments SET manifest_json = ? WHERE experiment_id = ?",
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                original.manifest.experiment_id,
            ),
        )

    with pytest.raises(Exception, match="strategy identity mismatch"):
        PreparedExperimentLoader(
            _runner(tmp_path, specification),
            experiment_id=original.manifest.experiment_id,
            specification_sha256=original.specification.fingerprint,
            strategy_id=original.specification.strategy_id,
        ).load()


def test_restart_loader_rejects_wrong_runner_and_configured_digest(
    tmp_path: Path, specification: object
) -> None:
    with pytest.raises(TypeError, match="runner must be ResearchRunner"):
        PreparedExperimentLoader(
            object(),  # type: ignore[arg-type]
            experiment_id="exp-" + "a" * 24,
            specification_sha256="b" * 64,
            strategy_id="strategy",
        )

    _, original = _prepare(tmp_path, specification)
    with pytest.raises(SpecificationError, match="specification identity mismatch"):
        PreparedExperimentLoader(
            _runner(tmp_path, specification),
            experiment_id=original.manifest.experiment_id,
            specification_sha256="b" * 64,
            strategy_id=original.specification.strategy_id,
        ).load()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("experiment_id", "invalid", "stored experiment_id is invalid"),
        ("specification_sha256", "invalid", "stored specification_sha256 is invalid"),
        ("strategy_id", "invalid space", "stored research strategy_id is invalid"),
    ],
)
def test_owner_identity_value_validation_fails_closed(field: str, value: str, message: str) -> None:
    values = {
        "experiment_id": "exp-" + "a" * 24,
        "specification_sha256": "b" * 64,
        "strategy_id": "strategy",
    }
    values[field] = value
    with pytest.raises(StateRepositoryError, match=message):
        ResearchExperimentOwnerIdentity(**values)


def test_owner_identity_api_rejects_wrong_types_and_missing_state(tmp_path: Path) -> None:
    identity = ResearchExperimentOwnerIdentity(
        experiment_id="exp-" + "a" * 24,
        specification_sha256="b" * 64,
        strategy_id="strategy",
    )
    assert PreparedResearchIdentity.from_owner_identity(identity).manifest.experiment_id == (
        identity.experiment_id
    )
    with pytest.raises(TypeError, match="identity must be ResearchExperimentOwnerIdentity"):
        PreparedResearchIdentity.from_owner_identity(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="repository must be SqliteExperimentRepository"):
        read_experiment_owner_identity(object(), identity.experiment_id)  # type: ignore[arg-type]
    repository = SqliteExperimentRepository(tmp_path / "missing.sqlite3")
    with pytest.raises(StateRepositoryError, match="experiment_id is invalid"):
        read_experiment_owner_identity(repository, "invalid")
    repository.path.unlink()
    with pytest.raises(StateRepositoryError, match="database is unavailable"):
        read_experiment_owner_identity(repository, identity.experiment_id)


def _manifest_payload(root: Path, experiment_id: str) -> dict[str, object]:
    path = root / "artifacts" / "experiments" / experiment_id / "manifest.json"
    value = canonical_json_loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.pop("status"), "experiment manifest fields are invalid"),
        (
            lambda value: value.__setitem__("scheduler_configuration", ()),
            "scheduler_configuration must be a string-keyed mapping",
        ),
        (lambda value: value.__setitem__("experiment_id", ""), "non-empty string"),
        (lambda value: value.__setitem__("parent_experiment_id", 1), "non-empty string"),
        (lambda value: value["git"].__setitem__("clean", "yes"), "git.clean must be bool"),
        (lambda value: value.__setitem__("schema_version", True), "schema_version must be int"),
        (
            lambda value: value.__setitem__("configuration_hashes", []),
            "configuration_hashes must be a canonical sequence",
        ),
        (lambda value: value.__setitem__("seeds", ("bad",)), "seeds must be int"),
        (lambda value: value.__setitem__("created_at", "today"), "canonical datetime"),
        (lambda value: value["split"].__setitem__("train", {}), "split.train fields"),
    ],
)
def test_manifest_decoder_rejects_noncanonical_typed_shapes(
    tmp_path: Path,
    specification: object,
    mutate: object,
    message: str,
) -> None:
    _, original = _prepare(tmp_path, specification)
    payload = deepcopy(_manifest_payload(tmp_path, original.manifest.experiment_id))
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(SpecificationError, match=message):
        _decode_manifest(payload)
