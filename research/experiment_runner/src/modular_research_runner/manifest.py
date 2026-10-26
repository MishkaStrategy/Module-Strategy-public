from __future__ import annotations

import platform
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256
from .models import ExperimentStatus, ResearchSpecification, SpecificationError
from .search import GeneratedConfiguration
from .splits import PlannedSplit


@dataclass(frozen=True, slots=True)
class GitProvenance:
    branch: str
    commit_sha: str
    clean: bool
    diff_sha256: str | None = None
    untracked_policy: str = "reject"

    def __post_init__(self) -> None:
        if len(self.commit_sha) != 40 or any(
            char not in "0123456789abcdef" for char in self.commit_sha
        ):
            raise SpecificationError("commit_sha must be 40 lowercase hexadecimal characters")
        if self.untracked_policy not in {"reject", "record"}:
            raise SpecificationError("unsupported untracked policy")
        if self.clean and self.diff_sha256 is not None:
            raise SpecificationError("clean working tree cannot have a diff hash")
        if not self.clean and self.diff_sha256 is None:
            raise SpecificationError("dirty working tree requires a diff hash")


@dataclass(frozen=True, slots=True)
class EnvironmentManifest:
    python_version: str
    platform: str
    dependency_lock_sha256: str | None

    @classmethod
    def current(cls, dependency_lock_sha256: str | None = None) -> EnvironmentManifest:
        return cls(
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            dependency_lock_sha256=dependency_lock_sha256,
        )


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    research_id: str
    parent_experiment_id: str | None
    specification_sha256: str
    search_space_sha256: str
    configuration_hashes: tuple[str, ...]
    dataset_hashes: Mapping[str, str]
    split: PlannedSplit
    strategy_id: str
    strategy_version: str
    module_versions: Mapping[str, str]
    module_api_version: str
    backtest_engine_version: str
    metric_schema_version: str
    git: GitProvenance
    environment: EnvironmentManifest
    scheduler_configuration: Mapping[str, Any]
    resource_limits: Mapping[str, Any]
    seeds: tuple[int, ...]
    timezone: str
    created_at: datetime
    status: ExperimentStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_hashes", MappingProxyType(dict(self.dataset_hashes)))
        object.__setattr__(self, "module_versions", MappingProxyType(dict(self.module_versions)))
        object.__setattr__(
            self,
            "scheduler_configuration",
            MappingProxyType(dict(self.scheduler_configuration)),
        )
        object.__setattr__(self, "resource_limits", MappingProxyType(dict(self.resource_limits)))
        if self.timezone != "UTC":
            raise SpecificationError("experiment timezone must be UTC")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise SpecificationError("manifest created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if self.schema_version != 1:
            raise SpecificationError("unsupported experiment manifest schema")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


class ExperimentManifestBuilder:
    def build(
        self,
        *,
        experiment_id: str,
        specification: ResearchSpecification,
        configurations: Sequence[GeneratedConfiguration],
        split: PlannedSplit,
        git: GitProvenance,
        backtest_engine_version: str,
        metric_schema_version: str,
        module_api_version: str,
        scheduler_configuration: Mapping[str, Any],
        environment: EnvironmentManifest | None = None,
        parent_experiment_id: str | None = None,
        created_at: datetime | None = None,
    ) -> ExperimentManifest:
        if not configurations:
            raise SpecificationError("manifest requires generated configurations")
        return ExperimentManifest(
            experiment_id=experiment_id,
            research_id=specification.research_id,
            parent_experiment_id=parent_experiment_id,
            specification_sha256=specification.fingerprint,
            search_space_sha256=specification.search_space.fingerprint,
            configuration_hashes=tuple(item.sha256 for item in configurations),
            dataset_hashes={item.dataset_id: item.sha256 for item in specification.datasets},
            split=split,
            strategy_id=specification.strategy_id,
            strategy_version=specification.strategy_version,
            module_versions=specification.module_versions,
            module_api_version=module_api_version,
            backtest_engine_version=backtest_engine_version,
            metric_schema_version=metric_schema_version,
            git=git,
            environment=environment or EnvironmentManifest.current(),
            scheduler_configuration=scheduler_configuration,
            resource_limits=specification.resource_limits,
            seeds=specification.seeds,
            timezone="UTC",
            created_at=(created_at or datetime.now(UTC)),
            status=ExperimentStatus.PLANNED,
        )
