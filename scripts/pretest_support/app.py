from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from .configs import write_configs
from .dataset import inspect_data, locate_data_file, read_summary
from .models import BASE_SHA, DataFacts, PretestError, Reporter, iso, run


def repository_check(reporter: Reporter, repo: Path, allow_dirty: bool) -> dict[str, object]:
    if not (repo / ".git").exists():
        reporter.fail("repository", f"not a Git checkout: {repo}")
        return {}
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    branch = run(["git", "branch", "--show-current"], repo).stdout.strip()
    ancestry = run(["git", "merge-base", "--is-ancestor", BASE_SHA, "HEAD"], repo)
    if ancestry.returncode:
        reporter.fail("ancestry", f"HEAD does not descend from {BASE_SHA[:12]}")
    else:
        reporter.passed("ancestry", f"HEAD {head[:12]} descends from canonical base")
    status = run(["git", "status", "--porcelain"], repo).stdout.strip()
    if status and not allow_dirty:
        reporter.fail("worktree", "uncommitted or untracked files are present")
    elif status:
        reporter.warn("worktree", "dirty worktree accepted by --allow-dirty")
    else:
        reporter.passed("worktree", f"clean on {branch or 'detached HEAD'}")
    return {"head": head, "branch": branch, "clean": not status}


def write_report(
    workspace: Path,
    reporter: Reporter,
    repository: Mapping[str, object],
    summary: Mapping[str, object] | None,
    data: DataFacts | None,
    generated: Mapping[str, str],
    disk: Mapping[str, object] | None = None,
) -> Path:
    payload = {
        "schema_version": "1",
        "generated_at": iso(datetime.now(tz=UTC)),
        "canonical_base_sha": BASE_SHA,
        "repository": dict(repository),
        "disk": dict(disk or {}),
        "summary": dict(summary or {}),
        "data": asdict(data) if data else None,
        "generated": dict(generated),
        "findings": [asdict(row) for row in reporter.findings],
        "failures": reporter.failures,
        "warnings": reporter.warnings,
        "ready_for_local_tests": reporter.failures == 0,
        "limitations": [
            "funding disabled",
            "historical exchange-info lineage not proven",
            "Trading Runner bootstrap remains application-specific",
            "production/live execution is out of scope",
        ],
    }
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "pretest-report.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate local BTCUSDT data and generate deterministic test configs."
    )
    result.add_argument("--dataset-dir", required=True, type=Path)
    result.add_argument("--repo-dir", type=Path, default=Path.cwd())
    result.add_argument("--workspace", type=Path)
    result.add_argument("--symbol", default="BTCUSDT")
    result.add_argument("--data-file", type=Path)
    result.add_argument("--allow-dirty", action="store_true")
    result.add_argument("--quick", action="store_true")
    result.add_argument("--run-validation", action="store_true")
    result.add_argument("--run-smoke", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo = args.repo_dir.expanduser().resolve()
    dataset = args.dataset_dir.expanduser().resolve()
    workspace = (
        args.workspace.expanduser().resolve()
        if args.workspace
        else repo / ".local-tests" / "pretest"
    )
    reporter = Reporter()
    repository = repository_check(reporter, repo, args.allow_dirty)
    if sys.version_info[:2] not in {(3, 12), (3, 13)}:
        reporter.fail("python", "use validated Python 3.12 or 3.13")
    else:
        reporter.passed("python", f"{sys.version_info.major}.{sys.version_info.minor}")
    disk: dict[str, object] = {}
    if dataset.is_dir():
        free = shutil.disk_usage(dataset).free / (1024**3)
        disk["free_gib"] = round(free, 3)
        if free < 10:
            reporter.fail("disk", f"only {free:.1f} GiB free")
        elif free < 20:
            reporter.warn("disk", f"{free:.1f} GiB free; 20 GiB recommended")
        else:
            reporter.passed("disk", f"{free:.1f} GiB free")
    else:
        reporter.fail("dataset", f"directory not found: {dataset}")

    summary_info: dict[str, object] | None = None
    data: DataFacts | None = None
    generated: dict[str, str] = {}
    try:
        summary_row, summary_info = read_summary(reporter, dataset, args.symbol)
        data_path = locate_data_file(dataset, args.symbol, args.data_file)
        reporter.passed("data file", str(data_path))
        data = inspect_data(reporter, data_path, summary_row, args.quick)
        generated = write_configs(reporter, repo, workspace, data, args.symbol)
    except (OSError, ValueError, PretestError) as exc:
        reporter.fail("preparation", str(exc))

    if args.run_validation and reporter.failures == 0:
        result = run(
            [
                sys.executable,
                "-m",
                "modular_backtest",
                "validate-data",
                "--config",
                generated["smoke"],
            ],
            repo,
        )
        if result.returncode:
            reporter.fail("validate-data", result.stdout.strip())
        else:
            reporter.passed("validate-data", result.stdout.strip())
    if args.run_smoke and reporter.failures == 0:
        result = run(["bash", generated["commands"]], repo)
        if result.returncode:
            reporter.fail("smoke ladder", result.stdout.strip())
        else:
            reporter.passed("smoke ladder", result.stdout.strip())

    report = write_report(workspace, reporter, repository, summary_info, data, generated, disk)
    print(f"Report: {report}")
    print(f"Failures: {reporter.failures}; warnings: {reporter.warnings}")
    return 1 if reporter.failures else 0
