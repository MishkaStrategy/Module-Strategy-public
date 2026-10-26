from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .artifacts import ArtifactReference, AtomicJsonArtifactStore
from .engine import BacktestRunReference
from .models import SpecificationError, freeze


@dataclass(frozen=True, slots=True)
class RunResultEnvelope:
    experiment_id: str
    run_id: str
    configuration_sha256: str
    stage_id: str
    stage_role: str
    split_id: str
    dataset_id: str
    dataset_sha256: str
    scenario_id: str
    seed: int
    engine: BacktestRunReference
    metrics_schema_version: str
    metrics_artifact: ArtifactReference | None
    metadata: Mapping[str, Any]
    created_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "experiment_id",
            "run_id",
            "configuration_sha256",
            "stage_id",
            "stage_role",
            "split_id",
            "dataset_id",
            "dataset_sha256",
            "scenario_id",
            "metrics_schema_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            raise SpecificationError("seed must be a non-negative integer")
        if not isinstance(self.engine, BacktestRunReference):
            raise SpecificationError("engine must be a BacktestRunReference")
        if self.engine.requested_run_id != self.run_id:
            raise SpecificationError("engine reference does not belong to the result envelope")
        if self.metrics_artifact is not None and not isinstance(
            self.metrics_artifact, ArtifactReference
        ):
            raise SpecificationError("metrics_artifact must be an ArtifactReference when provided")
        if not isinstance(self.metadata, Mapping):
            raise SpecificationError("metadata must be a mapping")
        if not isinstance(self.created_at, datetime):
            raise SpecificationError("result created_at must be a datetime")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise SpecificationError("result created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "metadata", MappingProxyType(dict(freeze(self.metadata))))
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in {1, 2}
        ):
            raise SpecificationError("unsupported result envelope schema")
        if self.schema_version == 1 and self.engine.effective_config_artifact is not None:
            raise SpecificationError("v1 result envelope cannot contain effective config proof")
        if self.schema_version == 2 and self.engine.effective_config_artifact is None:
            raise SpecificationError("v2 result envelope requires effective config proof")


class ResultSink(Protocol):
    def publish(self, envelope: RunResultEnvelope) -> ArtifactReference: ...


class LocalResultSink:
    """Writes a versioned ingestion envelope without recalculating metrics."""

    def __init__(self, root: Path) -> None:
        self.store = AtomicJsonArtifactStore(root)

    def publish(self, envelope: RunResultEnvelope) -> ArtifactReference:
        if not isinstance(envelope, RunResultEnvelope):
            raise SpecificationError("result sink requires a RunResultEnvelope")
        return self.store.write_json(
            f"results/{envelope.experiment_id}/{envelope.run_id}.json",
            envelope,
        )
