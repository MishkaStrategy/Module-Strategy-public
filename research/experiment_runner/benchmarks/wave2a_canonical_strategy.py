"""Run canonical Variant 2 through the durable Research Platform scheduler."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from modular_research_runner import (
    BacktestJob,
    ExperimentStatus,
    LocalResearchScheduler,
    ModularBacktestEngineAdapter,
    ResumePolicy,
    RetryPolicy,
    RunStatus,
    SqliteExperimentRepository,
    configuration_id,
    run_id,
    verify_output_directory,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
AUDITOR_ROOT = REPOSITORY_ROOT / "research" / "strategy_audit_validation"
HANDOFF_PATH = (
    REPOSITORY_ROOT / "reports" / "strategy-governance-wave2f" / "handoff-to-stream-a.json"
)
BASE_TIME = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CANONICAL_FILES = (
    "combined-ledger.csv",
    "frozen_1h_short-ledger.csv",
    "frozen_4h_short-ledger.csv",
    "fvg_rt_final_15m-ledger.csv",
    "summary.json",
)
_SAFE_FAILURE_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)?$")
_SENSITIVE_FAILURE_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "private_key",
    "secret",
    "token",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} must contain a JSON object")
    return value


def _subprocess_failure_message(stderr: str | None) -> str:
    if not stderr:
        return "authoritative parity subprocess failed without sanitized diagnostic"
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return "authoritative parity subprocess failed without sanitized diagnostic"
    candidate = lines[-1]
    failure_type, separator, detail = candidate.partition(":")
    failure_type = failure_type.strip()
    if _SAFE_FAILURE_TYPE_RE.fullmatch(failure_type) is None:
        failure_type = "subprocess"
    lowered = candidate.lower()
    unsafe = (
        len(candidate) > 600
        or "/" in candidate
        or "\\" in candidate
        or any(marker in lowered for marker in _SENSITIVE_FAILURE_MARKERS)
    )
    if unsafe or not separator:
        return f"authoritative parity subprocess failed: {failure_type}"
    return f"authoritative parity subprocess failed: {failure_type}: {detail.strip()}"


@dataclass(slots=True)
class _CanonicalEngine:
    input_path: Path
    provenance_path: Path
    generated_from_commit: str
    invocations: list[str] = field(default_factory=list)

    def run(self, job: BacktestJob) -> SimpleNamespace:
        self.invocations.append(job.run_id)
        output = job.output_directory / job.run_id
        if output.exists():
            shutil.rmtree(output)
        output.mkdir(parents=True)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(AUDITOR_ROOT)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.canonical_strategy_v2",
                "--input",
                str(self.input_path),
                "--dataset-provenance",
                str(self.provenance_path),
                "--output",
                str(output),
                "--generated-from-commit",
                self.generated_from_commit,
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        printed = json.loads(completed.stdout)
        summary = _json(output / "summary.json")
        if printed != summary:
            raise RuntimeError("canonical benchmark stdout and summary.json diverged")
        return SimpleNamespace(
            run_id=f"canonical-v2-{job.run_id}",
            output_directory=output,
            bars_processed=int(summary["dataset"]["row_count"]),
            restart_verified=True,
        )


def _run_authoritative_parity(
    *,
    input_path: Path,
    provenance_path: Path,
    baseline_output: Path,
    output: Path,
    generated_from_commit: str,
) -> dict[str, Any]:
    if output.exists():
        shutil.rmtree(output)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(AUDITOR_ROOT)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "benchmarks.authoritative_historical_parity",
                "--input",
                str(input_path),
                "--baseline",
                str(baseline_output),
                "--dataset-provenance",
                str(provenance_path),
                "--output",
                str(output),
                "--generated-from-commit",
                generated_from_commit,
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(_subprocess_failure_message(exc.stderr)) from None
    printed = json.loads(completed.stdout)
    report = _json(output / "parity-report.json")
    if printed != report:
        raise RuntimeError("authoritative parity stdout and parity-report.json diverged")
    if report.get("status") != "AUTHORITATIVE_CORE_BACKTEST_HISTORICAL_PARITY":
        raise RuntimeError("authoritative Core/Backtest parity gate did not pass")
    if report.get("ledger_exact") is not True or report.get("metrics_exact") is not True:
        raise RuntimeError("authoritative Core/Backtest parity evidence is incomplete")
    if report.get("restart_verified") is not True:
        raise RuntimeError("authoritative Core/Backtest restart evidence is incomplete")
    return report


class _RejectExecutionEngine:
    def run(self, job: BacktestJob) -> SimpleNamespace:
        raise RuntimeError(f"completed job was unexpectedly re-executed: {job.run_id}")


def _job(
    *,
    output_root: Path,
    experiment_id: str,
    dataset_id: str,
    repetition: int,
    handoff: dict[str, Any],
) -> BacktestJob:
    parameters = {
        "candidate_id": handoff["candidate_id"],
        "composition_hash": handoff["composition_hash"],
        "semantic_digest_contract": handoff["expected_strategy_reference"][
            "semantic_digest_contract"
        ],
    }
    config = configuration_id(parameters)
    identity = run_id(
        experiment_id=experiment_id,
        stage_id="canonical-full-corpus",
        split_id="2019-09-08_2026-07-19",
        configuration_sha256=config,
        scenario_id="paper-only",
        seed=1,
        repetition_index=repetition,
    )
    return BacktestJob(
        run_id=identity,
        experiment_id=experiment_id,
        stage_id="canonical-full-corpus",
        split_id="2019-09-08_2026-07-19",
        dataset_id=dataset_id,
        scenario_id="paper-only",
        seed=1,
        parameters=parameters,
        output_directory=output_root,
        stage_role="final-benchmark",
        repetition_index=repetition,
        metadata={"network_used_by_engine": False, "live_trading": False},
    )


def _validate_run(
    output: Path, expected: dict[str, Any], generated_from_commit: str
) -> dict[str, Any]:
    observed_names = {path.name for path in output.iterdir() if path.is_file()}
    if observed_names != set(CANONICAL_FILES):
        raise RuntimeError(f"unexpected canonical output inventory: {sorted(observed_names)}")
    summary = _json(output / "summary.json")
    if summary["generated_from_commit"] != generated_from_commit:
        raise RuntimeError("canonical benchmark commit provenance mismatch")
    if summary["candidate_id"] != expected["candidate_id"]:
        raise RuntimeError("candidate identity mismatch")
    if summary["composition_hash"] != expected["composition_hash"]:
        raise RuntimeError("composition identity mismatch")
    reference = expected["expected_strategy_reference"]
    if summary["semantic_digest_contract"] != reference["semantic_digest_contract"]:
        raise RuntimeError("semantic digest contract mismatch")
    if summary["semantic_digest"] != reference["semantic_digest"]:
        raise RuntimeError("semantic digest mismatch")
    expected_metrics = reference["metrics"] | {"trade_count": reference["combined_trade_count"]}
    if summary["metrics"] != expected_metrics:
        raise RuntimeError("canonical result metrics mismatch")
    combined = summary["ledgers"]["combined"]
    if combined["sha256"] != reference["combined_ledger_sha256"]:
        raise RuntimeError("combined ledger identity mismatch")
    component_expected = reference["component_ledger_sha256"]
    for module_id, item in summary["ledgers"]["components"].items():
        if item["sha256"] != component_expected[module_id]:
            raise RuntimeError(f"component ledger identity mismatch: {module_id}")
    return {
        "canonical_file_sha256": {name: _sha256(output / name) for name in CANONICAL_FILES},
        "semantic_digest": summary["semantic_digest"],
        "trade_count": summary["metrics"]["trade_count"],
    }


def run(
    *,
    input_path: Path,
    provenance_path: Path,
    work_root: Path,
    output_root: Path,
    generated_from_commit: str,
) -> dict[str, Any]:
    handoff = _json(HANDOFF_PATH)
    provenance = _json(provenance_path)
    dataset_id = str(provenance["content_sha256"])
    experiment_id = f"wave2a-canonical-v2-{handoff['candidate_id']}"
    state_path = work_root / "state.sqlite3"
    engine_outputs = work_root / "engine-output"
    work_root.mkdir(parents=True, exist_ok=True)
    engine_outputs.mkdir(parents=True, exist_ok=True)
    repository = SqliteExperimentRepository(state_path)
    repository.register_experiment(
        experiment_id=experiment_id,
        specification_sha256=hashlib.sha256(HANDOFF_PATH.read_bytes()).hexdigest(),
        manifest={
            "candidate_id": handoff["candidate_id"],
            "composition_hash": handoff["composition_hash"],
            "dataset_id": dataset_id,
            "paper_only": True,
            "schema_version": 1,
        },
        created_at=BASE_TIME,
    )
    jobs = tuple(
        _job(
            output_root=engine_outputs,
            experiment_id=experiment_id,
            dataset_id=dataset_id,
            repetition=repetition,
            handoff=handoff,
        )
        for repetition in range(2)
    )
    inserted = repository.register_jobs(jobs, max_attempts=1, created_at=BASE_TIME)
    if inserted != 2:
        raise RuntimeError(f"expected two new canonical jobs, inserted {inserted}")
    engine = _CanonicalEngine(input_path, provenance_path, generated_from_commit)
    scheduler = LocalResearchScheduler(
        repository=repository,
        engine=ModularBacktestEngineAdapter(lambda job: job, engine.run),
        retry_policy=RetryPolicy(max_attempts=1),
        resume_policy=ResumePolicy(lease_seconds=60, heartbeat_seconds=10),
        maximum_workers=1,
        poll_interval_seconds=0.001,
    )
    first = scheduler.run(experiment_id)
    if first.status is not ExperimentStatus.COMPLETED or len(engine.invocations) != 2:
        raise RuntimeError(
            "Research Platform did not execute both canonical repetitions exactly once"
        )
    for state in first.runs:
        if state.status is not RunStatus.SUCCEEDED or state.result is None:
            raise RuntimeError(f"canonical Research Platform run failed: {state.run_id}")
        if not verify_output_directory(state.result):
            raise RuntimeError(f"Research Platform output manifest failed: {state.run_id}")

    reopened = SqliteExperimentRepository(state_path)
    replay = LocalResearchScheduler(
        repository=reopened,
        engine=ModularBacktestEngineAdapter(lambda job: job, _RejectExecutionEngine().run),
        retry_policy=RetryPolicy(max_attempts=1),
        resume_policy=ResumePolicy(lease_seconds=60, heartbeat_seconds=10),
        maximum_workers=1,
        poll_interval_seconds=0.001,
    ).run(experiment_id)
    if replay.status is not ExperimentStatus.COMPLETED:
        raise RuntimeError("Research Platform restart replay did not preserve terminal success")

    run_evidence = []
    for job in jobs:
        output = engine_outputs / job.run_id
        run_evidence.append(
            {
                "run_id": job.run_id,
                **_validate_run(output, handoff, generated_from_commit),
            }
        )
    if run_evidence[0]["canonical_file_sha256"] != run_evidence[1]["canonical_file_sha256"]:
        raise RuntimeError("Research Platform canonical repetitions are not byte-identical")

    authoritative_output = work_root / "authoritative-parity"
    authoritative_report = _run_authoritative_parity(
        input_path=input_path,
        provenance_path=provenance_path,
        baseline_output=engine_outputs / jobs[0].run_id,
        output=authoritative_output,
        generated_from_commit=generated_from_commit,
    )

    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)
    for index, job in enumerate(jobs, start=1):
        target = output_root / f"repetition-{index}"
        shutil.copytree(engine_outputs / job.run_id, target)
    shutil.copytree(authoritative_output, output_root / "authoritative-parity")
    shutil.copy2(provenance_path, output_root / "dataset-provenance.json")
    report = {
        "authoritative_historical_parity": authoritative_report,
        "candidate_id": handoff["candidate_id"],
        "composition_hash": handoff["composition_hash"],
        "dataset_content_sha256": dataset_id,
        "generated_from_commit": generated_from_commit,
        "inserted_jobs": inserted,
        "live_trading": False,
        "network_used_by_research_engine": False,
        "paper_only": True,
        "repetitions": run_evidence,
        "restart_replay_engine_calls": 0,
        "semantic_digest": handoff["expected_strategy_reference"]["semantic_digest"],
        "semantic_digest_contract": handoff["expected_strategy_reference"][
            "semantic_digest_contract"
        ],
        "status": "CANONICAL_STRATEGY_RESEARCH_PLATFORM_REPRODUCED",
    }
    (output_root / "research-platform-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--dataset-provenance", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    arguments = parser.parse_args()
    report = run(
        input_path=arguments.input.resolve(strict=True),
        provenance_path=arguments.dataset_provenance.resolve(strict=True),
        work_root=arguments.work.resolve(),
        output_root=arguments.output.resolve(),
        generated_from_commit=arguments.generated_from_commit,
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
