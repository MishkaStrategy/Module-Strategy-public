from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .artifacts import ArtifactReference, AtomicJsonArtifactStore
from .models import ExperimentStatus
from .ranking import RankedCandidate
from .repository import SqliteExperimentRepository
from .state import FailureCategory, RunStatus


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    experiment_id: str
    status: ExperimentStatus
    generated_at: datetime
    run_counts: Mapping[str, int]
    failure_counts: Mapping[str, int]
    promoted_configuration_hashes: tuple[str, ...]
    schema_version: int = 1


class ExperimentReporter:
    def __init__(self, repository: SqliteExperimentRepository) -> None:
        self.repository = repository

    def build(
        self,
        experiment_id: str,
        *,
        ranked_candidates: Sequence[RankedCandidate] = (),
        generated_at: datetime | None = None,
    ) -> ExperimentReport:
        runs = self.repository.list_runs(experiment_id)
        counts: dict[str, int] = {status.value: 0 for status in RunStatus}
        failures: dict[str, int] = {category.value: 0 for category in FailureCategory}
        for run in runs:
            counts[run.status.value] += 1
            if run.failure is not None:
                failures[run.failure.category.value] += 1
        return ExperimentReport(
            experiment_id=experiment_id,
            status=self.repository.experiment_status(experiment_id),
            generated_at=(generated_at or datetime.now(UTC)).astimezone(UTC),
            run_counts={key: value for key, value in counts.items() if value},
            failure_counts={key: value for key, value in failures.items() if value},
            promoted_configuration_hashes=tuple(
                item.configuration_sha256 for item in ranked_candidates if item.promoted
            ),
        )

    @staticmethod
    def write_json(
        report: ExperimentReport,
        store: AtomicJsonArtifactStore,
    ) -> ArtifactReference:
        return store.write_json(f"reports/{report.experiment_id}/summary.json", report)

    @staticmethod
    def write_csv(report: ExperimentReport, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(("category", "name", "count"))
        for name, count in sorted(report.run_counts.items()):
            writer.writerow(("run_status", name, count))
        for name, count in sorted(report.failure_counts.items()):
            writer.writerow(("failure_category", name, count))
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(buffer.getvalue(), encoding="utf-8")
        temporary.replace(path)
