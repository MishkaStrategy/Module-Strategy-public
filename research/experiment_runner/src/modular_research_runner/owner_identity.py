from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from .canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_json_loads,
    canonical_sha256,
)
from .repository import SqliteExperimentRepository, StateRepositoryError

_EXPERIMENT_ID_RE = re.compile(r"^exp-[0-9a-f]{24}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ResearchExperimentOwnerIdentity:
    """Bounded durable identity read from the authoritative Research repository."""

    experiment_id: str
    specification_sha256: str
    strategy_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.experiment_id, str)
            or _EXPERIMENT_ID_RE.fullmatch(self.experiment_id) is None
        ):
            raise StateRepositoryError("stored experiment_id is invalid")
        if (
            not isinstance(self.specification_sha256, str)
            or _SHA256_RE.fullmatch(self.specification_sha256) is None
        ):
            raise StateRepositoryError("stored specification_sha256 is invalid")
        if (
            not isinstance(self.strategy_id, str)
            or _STRATEGY_ID_RE.fullmatch(self.strategy_id) is None
        ):
            raise StateRepositoryError("stored research strategy_id is invalid")


@dataclass(frozen=True, slots=True)
class ResearchSpecificationIdentity:
    """Minimal immutable specification projection required by System Acceptance."""

    fingerprint: str
    strategy_id: str


@dataclass(frozen=True, slots=True)
class ResearchManifestIdentity:
    """Minimal immutable manifest projection required by System Acceptance."""

    experiment_id: str


@dataclass(frozen=True, slots=True)
class PreparedResearchIdentity:
    """Protocol-shaped prepared identity without reconstructing or preparing an experiment."""

    specification: ResearchSpecificationIdentity
    manifest: ResearchManifestIdentity

    @classmethod
    def from_owner_identity(
        cls, identity: ResearchExperimentOwnerIdentity
    ) -> PreparedResearchIdentity:
        if not isinstance(identity, ResearchExperimentOwnerIdentity):
            raise TypeError("identity must be ResearchExperimentOwnerIdentity")
        return cls(
            specification=ResearchSpecificationIdentity(
                fingerprint=identity.specification_sha256,
                strategy_id=identity.strategy_id,
            ),
            manifest=ResearchManifestIdentity(experiment_id=identity.experiment_id),
        )


def read_experiment_owner_identity(
    repository: SqliteExperimentRepository,
    experiment_id: str,
) -> ResearchExperimentOwnerIdentity:
    """Read one experiment identity without mutating or preparing Research state.

    The query opens the repository database in SQLite read-only mode and cross-checks
    the authoritative row against its persisted canonical manifest. No caller-supplied
    digest or manifest content is accepted as identity truth.
    """

    if not isinstance(repository, SqliteExperimentRepository):
        raise TypeError("repository must be SqliteExperimentRepository")
    if not isinstance(experiment_id, str) or _EXPERIMENT_ID_RE.fullmatch(experiment_id) is None:
        raise StateRepositoryError("experiment_id is invalid")

    path = repository.path
    if path.is_symlink() or not path.is_file():
        raise StateRepositoryError("state repository database is unavailable")
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                "SELECT experiment_id, specification_sha256, manifest_json "
                "FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise StateRepositoryError("experiment identity could not be read") from exc

    if row is None:
        raise StateRepositoryError("experiment is not registered")
    stored_experiment_id = row["experiment_id"]
    specification_sha256 = row["specification_sha256"]
    manifest_json = row["manifest_json"]
    if not isinstance(manifest_json, str):
        raise StateRepositoryError("stored experiment manifest is invalid")
    try:
        manifest = canonical_json_loads(manifest_json)
    except CanonicalizationError as exc:
        raise StateRepositoryError("stored experiment manifest is invalid") from exc
    if not isinstance(manifest, Mapping):
        raise StateRepositoryError("stored experiment manifest is invalid")
    if manifest.get("experiment_id") != stored_experiment_id:
        raise StateRepositoryError("stored experiment manifest identity mismatch")
    if manifest.get("specification_sha256") != specification_sha256:
        raise StateRepositoryError("stored experiment specification identity mismatch")
    strategy_id = manifest.get("strategy_id")
    if not isinstance(strategy_id, str):
        raise StateRepositoryError("stored research strategy_id is invalid")
    return ResearchExperimentOwnerIdentity(
        experiment_id=stored_experiment_id,
        specification_sha256=specification_sha256,
        strategy_id=strategy_id,
    )


def read_experiment_owner_manifest_sha256(
    repository: SqliteExperimentRepository,
    experiment_id: str,
) -> str:
    """Hash the exact canonical manifest owned by the configured repository."""

    identity = read_experiment_owner_identity(repository, experiment_id)
    path = repository.path
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=30.0)
        try:
            row = connection.execute(
                "SELECT manifest_json FROM experiments WHERE experiment_id = ?",
                (identity.experiment_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise StateRepositoryError("experiment manifest could not be read") from exc
    if row is None or not isinstance(row[0], str):
        raise StateRepositoryError("stored experiment manifest is invalid")
    try:
        manifest = canonical_json_loads(row[0])
        if canonical_json_bytes(manifest) != row[0].encode("utf-8"):
            raise StateRepositoryError("stored experiment manifest is not canonical")
        return canonical_sha256(manifest)
    except CanonicalizationError as exc:
        raise StateRepositoryError("stored experiment manifest is invalid") from exc


__all__ = [
    "PreparedResearchIdentity",
    "ResearchExperimentOwnerIdentity",
    "ResearchManifestIdentity",
    "ResearchSpecificationIdentity",
    "read_experiment_owner_identity",
    "read_experiment_owner_manifest_sha256",
]
