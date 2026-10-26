"""Deterministic no-network SLO benchmark for the Research Platform."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import multiprocessing
import platform
import resource
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from modular_results_registry import RegistrationPackage, SQLiteRegistry

from modular_research_runner import (
    BacktestJob,
    CancellationToken,
    LocalResearchScheduler,
    ModularBacktestEngineAdapter,
    ResumePolicy,
    RetryPolicy,
    RunStatus,
    SqliteExperimentRepository,
    configuration_id,
    run_id,
)

BASE_TIME = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
CANDIDATE_ID = "strategy-candidate-954bb639db1fc250d14584e1"
DATASET_ID = "fixture-btcusdt-5m-governance-v1"
ROOT = Path(__file__).resolve().parents[3]


def _percentiles(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def rank(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, int(fraction * len(ordered) + 0.999999) - 1))
        return ordered[index]

    return {
        "count": float(len(ordered)),
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": rank(0.50),
        "p95_ms": rank(0.95),
        "p99_ms": rank(0.99),
        "max_ms": ordered[-1],
    }


def _rss() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _fds() -> int | None:
    for root in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return sum(1 for _ in root.iterdir())
        except OSError:
            pass
    return None


def _bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _event_latencies(repository: SqliteExperimentRepository, identity: str) -> tuple[float, float]:
    events = repository.events(identity)
    queued = next((e.occurred_at for e in events if e.to_status is RunStatus.QUEUED), None)
    running = next((e.occurred_at for e in events if e.to_status is RunStatus.RUNNING), None)
    terminal = next(
        (
            e.occurred_at
            for e in reversed(events)
            if e.to_status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
        ),
        None,
    )
    scheduling = 0.0 if queued is None or running is None else (running - queued).total_seconds()
    lifecycle = 0.0 if running is None or terminal is None else (terminal - running).total_seconds()
    return max(0.0, scheduling * 1000.0), max(0.0, lifecycle * 1000.0)


@dataclass(slots=True)
class _Engine:
    retry_ids: frozenset[str] = frozenset()
    payload_bytes: int = 0
    attempts: dict[str, int] = field(default_factory=dict, init=False)
    durations_ms: dict[str, float] = field(default_factory=dict, init=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def run(self, job: BacktestJob) -> SimpleNamespace:
        started = time.perf_counter_ns()
        with self.lock:
            attempt = self.attempts.get(job.run_id, 0) + 1
            self.attempts[job.run_id] = attempt
        if job.run_id in self.retry_ids and attempt == 1:
            raise TimeoutError("deterministic synthetic timeout")
        output = job.output_directory / job.run_id
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(
            json.dumps(
                {"candidate_id": CANDIDATE_ID, "dataset_id": job.dataset_id, "run_id": job.run_id},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if self.payload_bytes:
            (output / "payload.bin").write_bytes(b"x" * self.payload_bytes)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
        with self.lock:
            self.durations_ms[job.run_id] = self.durations_ms.get(job.run_id, 0.0) + elapsed
        return SimpleNamespace(
            run_id=f"engine-{job.run_id}",
            output_directory=output,
            bars_processed=1,
            restart_verified=True,
        )


def _job(root: Path, index: int, experiment_id: str) -> BacktestJob:
    parameters = {"candidate_id": CANDIDATE_ID, "threshold": index % 17}
    config = configuration_id(parameters)
    identity = run_id(
        experiment_id=experiment_id,
        stage_id="train",
        split_id="fixture",
        configuration_sha256=config,
        scenario_id="paper-only",
        seed=index + 1,
        repetition_index=0,
    )
    return BacktestJob(
        run_id=identity,
        experiment_id=experiment_id,
        stage_id="train",
        split_id="fixture",
        dataset_id=DATASET_ID,
        scenario_id="paper-only",
        seed=index + 1,
        parameters=parameters,
        output_directory=root,
        stage_role="train",
        metadata={"configuration_sha256": config},
    )


def _repository(path: Path, experiment_id: str) -> SqliteExperimentRepository:
    repository = SqliteExperimentRepository(path)
    repository.register_experiment(
        experiment_id=experiment_id,
        specification_sha256="a" * 64,
        manifest={"candidate_id": CANDIDATE_ID, "dataset_id": DATASET_ID, "schema_version": 1},
        created_at=BASE_TIME,
    )
    return repository


def _scheduler(
    root: Path, *, count: int, workers: int, retry_every: int = 0, payload_bytes: int = 0
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    experiment_id = f"exp-wave2a-{count}-{workers}-{retry_every}-{payload_bytes}"
    repository = _repository(root / "state.sqlite3", experiment_id)
    jobs = tuple(_job(root / "outputs", index, experiment_id) for index in range(count))
    admission: list[float] = []
    inserted = 0
    for job in jobs:
        started = time.perf_counter_ns()
        inserted += repository.register_jobs([job], max_attempts=2, created_at=BASE_TIME)
        admission.append((time.perf_counter_ns() - started) / 1_000_000.0)

    retry_ids = frozenset(
        job.run_id for index, job in enumerate(jobs) if retry_every and index % retry_every == 0
    )
    engine = _Engine(retry_ids=retry_ids, payload_bytes=payload_bytes)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda value: value, engine.run),
        retry_policy=RetryPolicy(
            max_attempts=2,
            initial_backoff_seconds=0.001,
            maximum_backoff_seconds=0.001,
        ),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=workers,
        poll_interval_seconds=0.001,
    )
    rss_before, fd_before = _rss(), _fds()
    children_before = len(multiprocessing.active_children())
    cpu_before, started = time.process_time(), time.perf_counter()
    summary = scheduler.run(experiment_id)
    elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu_before
    rss_after, fd_after = _rss(), _fds()

    scheduling: list[float] = []
    lifecycle: list[float] = []
    overhead: list[float] = []
    for state in summary.runs:
        schedule_ms, lifecycle_ms = _event_latencies(repository, state.run_id)
        scheduling.append(schedule_ms)
        lifecycle.append(lifecycle_ms)
        overhead.append(max(0.0, lifecycle_ms - engine.durations_ms.get(state.run_id, 0.0)))
    semantic = {
        "attempts": [state.attempt_count for state in summary.runs],
        "run_ids": [state.run_id for state in summary.runs],
        "statuses": [state.status.value for state in summary.runs],
    }
    artifact_bytes = _bytes(root / "outputs")
    return {
        "artifact_bytes": artifact_bytes,
        "artifact_bytes_per_experiment": artifact_bytes / max(1, count),
        "candidate_admission_latency": _percentiles(admission),
        "candidates_per_second": count / elapsed,
        "cpu_seconds": cpu_seconds,
        "execution_lifecycle_latency": _percentiles(lifecycle),
        "fd_growth": None if fd_before is None or fd_after is None else fd_after - fd_before,
        "inserted_jobs": inserted,
        "orchestration_overhead": _percentiles(overhead),
        "orphan_artifact_count": sum(1 for path in root.rglob("*.tmp") if path.exists()),
        "orphan_process_count": max(0, len(multiprocessing.active_children()) - children_before),
        "peak_rss_bytes": rss_after,
        "retry_count": sum(max(0, count - 1) for count in engine.attempts.values()),
        "rss_delta_bytes": max(0, rss_after - rss_before),
        "scheduling_latency": _percentiles(scheduling),
        "semantic_digest": _digest(semantic),
        "status": summary.status.value,
        "temporary_bytes": _bytes(root),
        "wall_seconds": elapsed,
        "workers": workers,
    }


def _duplicates(root: Path, attempts: int) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    repository = _repository(root / "state.sqlite3", "exp-wave2a-duplicate")
    job = _job(root / "outputs", 0, "exp-wave2a-duplicate")
    repository.register_jobs([job], max_attempts=1, created_at=BASE_TIME)
    latencies: list[float] = []
    suppressed = 0
    for _ in range(attempts):
        started = time.perf_counter_ns()
        suppressed += int(
            repository.register_jobs([job], max_attempts=1, created_at=BASE_TIME) == 0
        )
        latencies.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return {
        "attempts": attempts,
        "duplicate_suppression_count": suppressed,
        "latency": _percentiles(latencies),
        "semantic_digest": _digest({"run_id": job.run_id, "suppressed": suppressed}),
    }


def _invalid(root: Path, attempts: int) -> dict[str, Any]:
    output = root / "invalid-output"
    latencies: list[float] = []
    rejected = 0
    for index in range(attempts):
        started = time.perf_counter_ns()
        try:
            BacktestJob(
                run_id="",
                experiment_id="exp-invalid",
                stage_id="train",
                split_id="fixture",
                dataset_id=DATASET_ID,
                scenario_id="paper-only",
                seed=index,
                parameters={},
                output_directory=output,
            )
        except Exception:  # noqa: BLE001 - exercises the public validation boundary.
            rejected += 1
        latencies.append((time.perf_counter_ns() - started) / 1_000_000.0)
    side_effects = int(output.exists())
    return {
        "attempts": attempts,
        "backtest_side_effects": side_effects,
        "latency": _percentiles(latencies),
        "rejected": rejected,
        "semantic_digest": _digest({"rejected": rejected, "side_effects": side_effects}),
    }


def _package_factory() -> Callable[..., RegistrationPackage]:
    path = ROOT / "modules" / "results_aggregator" / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_wave2a_results_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Results Aggregator fixture module is unavailable")
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    factory = getattr(module, "make_package", None)
    if not callable(factory):
        raise RuntimeError("Results Aggregator package factory is unavailable")
    return factory


def _publication(root: Path, count: int) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    make_package = _package_factory()

    def package_for(index: int) -> RegistrationPackage:
        package = make_package(
            package_id=f"package-wave2a-{index:06d}",
            experiment_id=f"experiment-wave2a-{index:06d}",
            configuration_value=index,
            run_id=f"run-wave2a-{index:06d}",
            artifact_id=f"artifact-wave2a-{index:06d}",
            metric_value=float(index),
        )
        evidence = replace(package.evidence[0], evidence_id=f"evidence-wave2a-{index:06d}")
        return replace(package, evidence=(evidence,))

    packages = tuple(package_for(index) for index in range(count))
    latencies: list[float] = []
    registered = 0
    rss_before, fd_before, started = _rss(), _fds(), time.perf_counter()
    with SQLiteRegistry(root / "registry.sqlite3") as registry:
        for package in packages:
            item_started = time.perf_counter_ns()
            outcome = registry.register_package(package)
            latencies.append((time.perf_counter_ns() - item_started) / 1_000_000.0)
            registered += outcome.registered_entities
        duplicate_started = time.perf_counter_ns()
        duplicate = registry.register_package(packages[0])
        duplicate_latency = (time.perf_counter_ns() - duplicate_started) / 1_000_000.0
        revision = registry.revision()
    elapsed, rss_after, fd_after = time.perf_counter() - started, _rss(), _fds()
    semantic = {
        "package_ids": [package.package_id for package in packages],
        "registered": registered,
        "revision": revision,
    }
    return {
        "duplicate_publication_latency_ms": duplicate_latency,
        "duplicate_suppression_count": duplicate.duplicate_entities,
        "fd_growth": None if fd_before is None or fd_after is None else fd_after - fd_before,
        "packages_per_second": count / elapsed,
        "peak_rss_bytes": rss_after,
        "publication_latency": _percentiles(latencies),
        "registered_entities": registered,
        "registry_bytes": _bytes(root),
        "revision": revision,
        "rss_delta_bytes": max(0, rss_after - rss_before),
        "semantic_digest": _digest(semantic),
        "wall_seconds": elapsed,
    }


def _publication_conflict(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    package = _package_factory()()
    conflict = replace(
        package,
        experiments=(replace(package.experiments[0], title="conflicting immutable title"),),
    )
    rejected = False
    with SQLiteRegistry(root / "registry.sqlite3") as registry:
        registry.register_package(package)
        before = registry.revision()
        try:
            registry.register_package(conflict)
        except Exception:  # noqa: BLE001 - immutable conflict types remain registry-owned.
            rejected = True
        after = registry.revision()
    return {
        "partial_publication_visible": after != before,
        "rejected": rejected,
        "revision_after": after,
        "revision_before": before,
        "semantic_digest": _digest({"after": after, "before": before, "rejected": rejected}),
    }


def _restart(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    experiment_id = "exp-wave2a-restart"
    path = root / "state.sqlite3"
    repository = _repository(path, experiment_id)
    jobs = tuple(_job(root / "outputs", index, experiment_id) for index in range(10))
    repository.register_jobs(jobs, max_attempts=2, created_at=BASE_TIME)
    stale = datetime.now(UTC) - timedelta(seconds=10)
    repository.queue_planned(experiment_id, at=stale)
    assert (
        repository.claim_next(
            experiment_id=experiment_id,
            worker_id="interrupted-worker",
            session_id="interrupted-session",
            lease_token="interrupted-token",
            at=stale,
            lease_seconds=1,
        )
        is not None
    )
    reopened = SqliteExperimentRepository(path)
    started = time.perf_counter()
    recovered = reopened.recover_expired_leases(experiment_id=experiment_id, at=datetime.now(UTC))
    recovery_ms = (time.perf_counter() - started) * 1000.0
    summary = LocalResearchScheduler(
        repository=reopened,
        engine=ModularBacktestEngineAdapter(lambda value: value, _Engine().run),
        retry_policy=RetryPolicy(max_attempts=2),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=2,
        poll_interval_seconds=0.001,
    ).run(experiment_id)
    run_ids = [state.run_id for state in summary.runs]
    semantic = {
        "attempts": [state.attempt_count for state in summary.runs],
        "recovered": recovered,
        "run_ids": run_ids,
        "statuses": [state.status.value for state in summary.runs],
    }
    return {
        "ordering_preserved": run_ids == sorted(run_ids),
        "recovered_leases": recovered,
        "recovery_time_ms": recovery_ms,
        "semantic_digest": _digest(semantic),
        "status": summary.status.value,
    }


def _cancellation(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    experiment_id = "exp-wave2a-cancel"
    repository = _repository(root / "state.sqlite3", experiment_id)
    repository.register_jobs(
        tuple(_job(root / "outputs", index, experiment_id) for index in range(10)),
        max_attempts=1,
        created_at=BASE_TIME,
    )
    token = CancellationToken()
    token.cancel()
    children_before, started = len(multiprocessing.active_children()), time.perf_counter()
    summary = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda value: value, _Engine().run),
        retry_policy=RetryPolicy(max_attempts=1),
        resume_policy=ResumePolicy(lease_seconds=30, heartbeat_seconds=5),
        maximum_workers=2,
        poll_interval_seconds=0.001,
    ).run(experiment_id, cancellation=token)
    cleanup_ms = (time.perf_counter() - started) * 1000.0
    semantic = {
        "run_ids": [state.run_id for state in summary.runs],
        "statuses": [state.status.value for state in summary.runs],
    }
    return {
        "cancellation_cleanup_ms": cleanup_ms,
        "canceled_runs": sum(state.status is RunStatus.CANCELED for state in summary.runs),
        "orphan_artifact_count": sum(1 for path in root.rglob("*.tmp") if path.exists()),
        "orphan_process_count": max(0, len(multiprocessing.active_children()) - children_before),
        "semantic_digest": _digest(semantic),
        "status": summary.status.value,
    }


def _variance(samples: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(sample[key]) for sample in samples]
    mean = statistics.fmean(values)
    return {
        "coefficient_of_variation_percent": (
            0.0 if mean == 0.0 else statistics.pstdev(values) / mean * 100.0
        ),
        "mean": mean,
        "sample_count": len(values),
        "values": values,
    }


def run_baseline(*, output_path: Path, profile: str = "baseline") -> dict[str, Any]:
    counts = (1, 10, 100, 1000) if profile == "baseline" else (1, 10)
    repeats, scale = (3, 1000) if profile == "baseline" else (1, 10)
    with tempfile.TemporaryDirectory(prefix="wave2a-research-") as temporary:
        root = Path(temporary)
        workloads: dict[str, Any] = {}
        for count in counts:
            workloads[f"scheduler-sequential-{count}"] = _scheduler(
                root / f"sequential-{count}", count=count, workers=1
            )
            workloads[f"scheduler-bounded-{count}"] = _scheduler(
                root / f"bounded-{count}", count=count, workers=min(4, count)
            )
            workloads[f"publication-{count}"] = _publication(root / f"publication-{count}", count)
        workloads.update(
            {
                "repeated-identical-1000": _duplicates(root / "duplicates", scale),
                "mostly-invalid-1000": _invalid(root / "invalid", scale),
                "retry-100": _scheduler(
                    root / "retry",
                    count=100 if profile == "baseline" else 10,
                    workers=4,
                    retry_every=10,
                ),
                "large-artifact": _scheduler(
                    root / "large", count=1, workers=1, payload_bytes=1_048_576
                ),
                "multiple-small-artifacts": _scheduler(
                    root / "small", count=10, workers=2, payload_bytes=1_024
                ),
                "publication-conflict": _publication_conflict(root / "conflict"),
                "restart-midway": _restart(root / "restart"),
                "cancellation": _cancellation(root / "cancellation"),
            }
        )
        scheduler_repeats = [
            _scheduler(root / f"repeat-s-{index}", count=scale, workers=4)
            for index in range(repeats)
        ]
        publication_repeats = [
            _publication(root / f"repeat-p-{index}", scale) for index in range(repeats)
        ]
        scheduler_digests = sorted({sample["semantic_digest"] for sample in scheduler_repeats})
        publication_digests = sorted({sample["semantic_digest"] for sample in publication_repeats})
        report: dict[str, Any] = {
            "environment": {
                "implementation": platform.python_implementation(),
                "machine": platform.machine(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            },
            "profile": profile,
            "report_id": "research-platform-wave2a-baseline-v1",
            "schema_version": 1,
            "stream_f_contract": {
                "candidate_id": CANDIDATE_ID,
                "canonical_strategy_execution_claimed": False,
                "canonical_strategy_status": "INCOMPLETE_FAIL_CLOSED",
                "workload_generator_contract": "strategy-governance-synthetic-v1",
            },
            "variance": {
                "publication_1000_packages_per_second": _variance(
                    publication_repeats, "packages_per_second"
                ),
                "publication_semantic_digests": publication_digests,
                "scheduler_1000_candidates_per_second": _variance(
                    scheduler_repeats, "candidates_per_second"
                ),
                "scheduler_semantic_digests": scheduler_digests,
            },
            "workload_contract": {
                "dataset_id": DATASET_ID,
                "exchange_credentials_used": False,
                "id": "research-platform-wave2a-synthetic-v1",
                "live_trading_used": False,
                "network_used": False,
                "paper_only": True,
                "raw_dataset_published": False,
            },
            "workloads": workloads,
        }
        report["semantic_digest"] = _digest(
            {
                "profile": profile,
                "publication_digests": publication_digests,
                "scheduler_digests": scheduler_digests,
                "workload_digests": {
                    name: value["semantic_digest"]
                    for name, value in workloads.items()
                    if "semantic_digest" in value
                },
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("baseline", "smoke"), default="baseline")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    report = run_baseline(output_path=arguments.output, profile=arguments.profile)
    print(json.dumps({"digest": report["semantic_digest"], "output": str(arguments.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
