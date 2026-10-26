"""Verified Wave 2A baseline entry point with immutable-conflict visibility checks."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from modular_results_registry import SQLiteRegistry

from . import wave2a_research_platform_slo as base


def _verified_publication_conflict(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    package = base._package_factory()
    original_package = package()
    original = original_package.experiments[0]
    conflicting_title = "conflicting immutable title"
    conflict = replace(
        original_package,
        experiments=(replace(original, title=conflicting_title),),
    )
    rejected = False
    with SQLiteRegistry(root / "registry.sqlite3") as registry:
        registry.register_package(original_package)
        revision_before = registry.revision()
        try:
            registry.register_package(conflict)
        except Exception:  # noqa: BLE001 - conflict taxonomy remains registry-owned.
            rejected = True
        revision_after = registry.revision()
        persisted = registry.get_payload("experiment", original.experiment_id)
        quarantine_count = registry.quarantine_count()

    original_preserved = persisted is not None and persisted.get("title") == original.title
    conflicting_visible = persisted is not None and persisted.get("title") == conflicting_title
    semantic = {
        "conflicting_visible": conflicting_visible,
        "original_preserved": original_preserved,
        "quarantine_count": quarantine_count,
        "rejected": rejected,
        "revision_after": revision_after,
        "revision_before": revision_before,
    }
    return {
        "audit_revision_advanced": revision_after > revision_before,
        "conflicting_entity_visible": conflicting_visible,
        "original_entity_preserved": original_preserved,
        "quarantine_count": quarantine_count,
        "rejected": rejected,
        "revision_after": revision_after,
        "revision_before": revision_before,
        "semantic_digest": base._digest(semantic),
    }


def _report_digest(report: dict[str, Any]) -> str:
    workloads = report["workloads"]
    variance = report["variance"]
    return base._digest(
        {
            "profile": report["profile"],
            "publication_digests": variance["publication_semantic_digests"],
            "scheduler_digests": variance["scheduler_semantic_digests"],
            "workload_digests": {
                name: value["semantic_digest"]
                for name, value in workloads.items()
                if "semantic_digest" in value
            },
        }
    )


def run_verified_baseline(*, output_path: Path, profile: str = "baseline") -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(output_path.parent)
    try:
        report = base.run_baseline(output_path=output_path, profile=profile)
    finally:
        tempfile.tempdir = previous_tempdir

    with tempfile.TemporaryDirectory(
        prefix=".wave2a-conflict-", dir=output_path.parent
    ) as temporary:
        report["workloads"]["publication-conflict"] = _verified_publication_conflict(
            Path(temporary)
        )
    report["semantic_digest"] = _report_digest(report)
    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("baseline", "smoke"), default="baseline")
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    report = run_verified_baseline(output_path=arguments.output, profile=arguments.profile)
    print(json.dumps({"digest": report["semantic_digest"], "output": str(arguments.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
