"""Fail-closed reconstruction of an already-prepared Research experiment."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .artifacts import ArtifactError
from .canonical import (
    canonical_json_bytes,
    canonical_json_loads,
    canonical_sha256,
    canonical_value,
)
from .identity import ExperimentIdentityInputs
from .manifest import EnvironmentManifest, ExperimentManifest, GitProvenance
from .models import ExperimentStatus, SpecificationError, TimeRange
from .owner_identity import (
    read_experiment_owner_identity,
    read_experiment_owner_manifest_sha256,
)
from .runner import PreparedExperiment, ResearchRunner
from .specification import research_specification_from_mapping
from .splits import PlannedSplit


class PreparedExperimentLoader:
    """Reconstruct one configured PreparedExperiment without calling prepare()."""

    def __init__(
        self,
        runner: ResearchRunner,
        *,
        experiment_id: str,
        specification_sha256: str,
        strategy_id: str,
    ) -> None:
        if not isinstance(runner, ResearchRunner):
            raise TypeError("runner must be ResearchRunner")
        self._runner = runner
        self._experiment_id = experiment_id
        self._specification_sha256 = specification_sha256
        self._strategy_id = strategy_id

    def load(self) -> PreparedExperiment:
        identity = read_experiment_owner_identity(
            self._runner.repository,
            self._experiment_id,
        )
        if identity.specification_sha256 != self._specification_sha256:
            raise SpecificationError("prepared Research specification identity mismatch")
        if identity.strategy_id != self._strategy_id:
            raise SpecificationError("prepared Research strategy identity mismatch")

        root = f"experiments/{self._experiment_id}"
        specification_reference = self._runner.artifact_store.reference(
            f"{root}/specification.json"
        )
        manifest_reference = self._runner.artifact_store.reference(f"{root}/manifest.json")
        plan_reference = self._runner.artifact_store.reference(f"{root}/plan.json")
        try:
            specification_payload = _load_canonical_artifact(
                self._runner.artifact_store.root / specification_reference.relative_path
            )
            manifest_payload = _load_canonical_artifact(
                self._runner.artifact_store.root / manifest_reference.relative_path
            )
            plan_payload = _load_canonical_artifact(
                self._runner.artifact_store.root / plan_reference.relative_path
            )
        except (ArtifactError, OSError, ValueError) as exc:
            raise SpecificationError(
                "prepared Research artifacts are unavailable or corrupt"
            ) from exc

        specification = research_specification_from_mapping(
            _configuration_value(specification_payload)
        )
        if specification.fingerprint != identity.specification_sha256:
            raise SpecificationError("prepared Research specification digest mismatch")
        if specification.strategy_id != identity.strategy_id:
            raise SpecificationError("prepared Research specification strategy mismatch")
        derived_id = ExperimentIdentityInputs(
            specification=specification,
            backtest_engine_version=self._runner.backtest_engine_version,
            metric_schema_version=self._runner.metric_schema_version,
            module_api_version=self._runner.module_api_version,
        ).experiment_id()
        if derived_id != identity.experiment_id:
            raise SpecificationError("prepared Research experiment identity mismatch")

        plan = self._runner.plan_builder.build(
            experiment_id=identity.experiment_id,
            specification=specification,
        )
        if canonical_value(plan) != canonical_value(plan_payload):
            raise SpecificationError("prepared Research plan artifact mismatch")
        manifest = _decode_manifest(manifest_payload)
        if canonical_value(manifest) != canonical_value(manifest_payload):
            raise SpecificationError("prepared Research manifest artifact mismatch")
        if canonical_sha256(manifest_payload) != read_experiment_owner_manifest_sha256(
            self._runner.repository,
            identity.experiment_id,
        ):
            raise SpecificationError("prepared Research repository manifest mismatch")
        if (
            manifest.experiment_id != identity.experiment_id
            or manifest.specification_sha256 != identity.specification_sha256
            or manifest.strategy_id != identity.strategy_id
            or manifest.configuration_hashes != tuple(item.sha256 for item in plan.configurations)
            or manifest.split != plan.split
        ):
            raise SpecificationError("prepared Research manifest consistency mismatch")
        return PreparedExperiment(
            specification=specification,
            manifest=manifest,
            plan=plan,
            specification_artifact=specification_reference,
            manifest_artifact=manifest_reference,
            plan_artifact=plan_reference,
        )


def _decode_manifest(value: object) -> ExperimentManifest:
    data = _mapping(
        value,
        {
            "experiment_id",
            "research_id",
            "parent_experiment_id",
            "specification_sha256",
            "search_space_sha256",
            "configuration_hashes",
            "dataset_hashes",
            "split",
            "strategy_id",
            "strategy_version",
            "module_versions",
            "module_api_version",
            "backtest_engine_version",
            "metric_schema_version",
            "git",
            "environment",
            "scheduler_configuration",
            "resource_limits",
            "seeds",
            "timezone",
            "created_at",
            "status",
            "schema_version",
        },
        "experiment manifest",
    )
    split = _mapping(
        data["split"],
        {"split_id", "train", "validation", "test", "final_untouched_test"},
        "experiment split",
    )
    git = _mapping(
        data["git"],
        {"branch", "commit_sha", "clean", "diff_sha256", "untracked_policy"},
        "experiment git provenance",
    )
    environment = _mapping(
        data["environment"],
        {"python_version", "platform", "dependency_lock_sha256"},
        "experiment environment",
    )
    return ExperimentManifest(
        experiment_id=_text(data["experiment_id"], "experiment_id"),
        research_id=_text(data["research_id"], "research_id"),
        parent_experiment_id=_optional_text(data["parent_experiment_id"], "parent_experiment_id"),
        specification_sha256=_text(data["specification_sha256"], "specification_sha256"),
        search_space_sha256=_text(data["search_space_sha256"], "search_space_sha256"),
        configuration_hashes=_text_tuple(data["configuration_hashes"], "configuration_hashes"),
        dataset_hashes=_text_mapping(data["dataset_hashes"], "dataset_hashes"),
        split=PlannedSplit(
            split_id=_text(split["split_id"], "split_id"),
            train=_range(split["train"], "split.train"),
            validation=_range(split["validation"], "split.validation"),
            test=_range(split["test"], "split.test"),
            final_untouched_test=(
                None
                if split["final_untouched_test"] is None
                else _range(split["final_untouched_test"], "split.final_untouched_test")
            ),
        ),
        strategy_id=_text(data["strategy_id"], "strategy_id"),
        strategy_version=_text(data["strategy_version"], "strategy_version"),
        module_versions=_text_mapping(data["module_versions"], "module_versions"),
        module_api_version=_text(data["module_api_version"], "module_api_version"),
        backtest_engine_version=_text(data["backtest_engine_version"], "backtest_engine_version"),
        metric_schema_version=_text(data["metric_schema_version"], "metric_schema_version"),
        git=GitProvenance(
            branch=_text(git["branch"], "git.branch"),
            commit_sha=_text(git["commit_sha"], "git.commit_sha"),
            clean=_bool(git["clean"], "git.clean"),
            diff_sha256=_optional_text(git["diff_sha256"], "git.diff_sha256"),
            untracked_policy=_text(git["untracked_policy"], "git.untracked_policy"),
        ),
        environment=EnvironmentManifest(
            python_version=_text(environment["python_version"], "environment.python_version"),
            platform=_text(environment["platform"], "environment.platform"),
            dependency_lock_sha256=_optional_text(
                environment["dependency_lock_sha256"],
                "environment.dependency_lock_sha256",
            ),
        ),
        scheduler_configuration=_mapping_value(
            data["scheduler_configuration"], "scheduler_configuration"
        ),
        resource_limits=_mapping_value(data["resource_limits"], "resource_limits"),
        seeds=_int_tuple(data["seeds"], "seeds"),
        timezone=_text(data["timezone"], "timezone"),
        created_at=_datetime(data["created_at"], "created_at"),
        status=ExperimentStatus(_text(data["status"], "status")),
        schema_version=_int(data["schema_version"], "schema_version"),
    )


def _mapping(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SpecificationError(f"{name} fields are invalid")
    if any(not isinstance(key, str) for key in value):
        raise SpecificationError(f"{name} keys are invalid")
    return value


def _mapping_value(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SpecificationError(f"{name} must be a string-keyed mapping")
    return value


def _text_mapping(value: object, name: str) -> Mapping[str, str]:
    mapping = _mapping_value(value, name)
    return {
        _text(key, f"{name} key"): _text(item, f"{name} value") for key, item in mapping.items()
    }


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecificationError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise SpecificationError(f"{name} must be bool")
    return value


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecificationError(f"{name} must be int")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise SpecificationError(f"{name} must be a canonical sequence")
    return value


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    return tuple(_text(item, name) for item in _sequence(value, name))


def _int_tuple(value: object, name: str) -> tuple[int, ...]:
    return tuple(_int(item, name) for item in _sequence(value, name))


def _datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SpecificationError(f"{name} must be a canonical datetime")
    return value


def _range(value: object, name: str) -> TimeRange:
    data = _mapping(value, {"start", "end"}, name)
    return TimeRange(
        start=_datetime(data["start"], f"{name}.start"),
        end=_datetime(data["end"], f"{name}.end"),
    )


def _configuration_value(value: object) -> object:
    """Return the list-shaped representation accepted by the strict config decoder."""

    if isinstance(value, tuple):
        return [_configuration_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _configuration_value(item) for key, item in value.items() if item is not None}
    return value


def _load_canonical_artifact(path: object) -> object:
    if not isinstance(path, Path):
        raise TypeError("artifact path must be Path")
    raw = path.read_bytes()
    value = canonical_json_loads(raw)
    if canonical_json_bytes(value) != raw:
        raise SpecificationError("prepared Research artifact is not canonical")
    return value


__all__ = ["PreparedExperimentLoader"]
