#!/usr/bin/env python3
"""Collect and compare pytest node IDs for isolated monorepo suites."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from test_suites import (
    DEFAULT_MANIFEST,
    ManifestError,
    Suite,
    load_manifest,
    preflight,
    resolve_selection,
)

ROOT = Path(__file__).resolve().parents[1]
COLLECTION_RECORDER = ROOT / "scripts" / "pytest_collect_ids.py"
COLLECTION_BOOTSTRAP = "import runpy, sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')"
DEFAULT_RELOCATION_MANIFEST = DEFAULT_MANIFEST
REPORT_SCHEMA_VERSION = 1
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class NodeIdRelocationManifest:
    audited_base_sha: str
    relocations: Mapping[str, str]
    parameter_id_migrations: Mapping[str, str]


def extract_node_ids(output: str) -> tuple[str, ...]:
    """Extract node IDs from quiet pytest output for legacy report compatibility."""
    node_ids: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "::" not in line or line.startswith(("=", "ERROR", "WARNING")):
            continue
        first_segment = line.split("::", 1)[0]
        if not first_segment.endswith(".py"):
            continue
        node_ids.append(line)
    return tuple(dict.fromkeys(node_ids))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _suite_command(suite: Suite, recorder_output: Path, passthrough: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-c",
        COLLECTION_BOOTSTRAP,
        str(COLLECTION_RECORDER),
        "--output",
        str(recorder_output),
        "--",
        "--collect-only",
        *suite.pytest_args,
        *suite.paths,
        *passthrough,
    ]


def _load_recorder_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"invalid pytest collection recorder output: {path}")
    node_ids = payload.get("node_ids")
    collection_errors = payload.get("collection_errors")
    if not isinstance(node_ids, list) or not all(isinstance(item, str) for item in node_ids):
        raise ValueError(f"invalid node IDs in pytest collection recorder output: {path}")
    if not isinstance(collection_errors, list):
        raise ValueError(f"invalid collection errors in pytest collection recorder output: {path}")
    return payload


def collect_suite(
    suite: Suite,
    *,
    environment: dict[str, str],
    timeout_seconds: float,
    passthrough: Sequence[str],
) -> dict[str, Any]:
    cwd = ROOT / suite.working_directory
    started = time.monotonic()
    timed_out = False
    recorder_error: str | None = None
    node_ids: tuple[str, ...] = ()
    collection_errors: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix=f"pytest-collect-{suite.name}-") as directory:
        recorder_output = Path(directory) / "collection.json"
        command = _suite_command(suite, recorder_output, passthrough)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""

        if recorder_output.exists():
            try:
                recorder = _load_recorder_payload(recorder_output)
                node_ids = tuple(dict.fromkeys(recorder["node_ids"]))
                collection_errors = list(recorder["collection_errors"])
                recorded_exit = recorder.get("exit_code")
                if isinstance(recorded_exit, int) and not timed_out and recorded_exit != returncode:
                    recorder_error = (
                        f"recorder exit mismatch: subprocess={returncode} recorded={recorded_exit}"
                    )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                recorder_error = f"{type(exc).__name__}: {exc}"
        elif not timed_out:
            recorder_error = "pytest collection recorder did not create output"

    duration = time.monotonic() - started
    combined = stdout + "\n" + stderr
    return {
        "suite": suite.name,
        "component": suite.component,
        "working_directory": suite.working_directory.as_posix(),
        "paths": list(suite.paths),
        "pytest_args": list(suite.pytest_args),
        "command": command,
        "exit_code": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 6),
        "collected": len(node_ids),
        "node_ids": list(node_ids),
        "collection_errors": collection_errors,
        "recorder_error": recorder_error,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "output_tail": combined.splitlines()[-80:],
    }


def build_collection_report(
    selection: Sequence[str],
    *,
    manifest_path: Path,
    timeout_seconds: float,
    keep_going: bool,
    passthrough: Sequence[str],
) -> tuple[dict[str, Any], int]:
    manifest = load_manifest(manifest_path)
    suites = resolve_selection(manifest, selection)
    if not preflight(suites):
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "audited_base_sha": manifest.audited_base_sha,
            "status": "dependency-preflight-failed",
            "selected_suites": [suite.name for suite in suites],
            "suites": [],
        }, 2

    environment = os.environ.copy()
    environment.setdefault("TZ", manifest.timezone)
    environment.setdefault("PYTHONHASHSEED", manifest.pythonhashseed)
    suite_reports: list[dict[str, Any]] = []
    for suite in suites:
        print(f"[collect] {suite.name}", flush=True)
        report = collect_suite(
            suite,
            environment=environment,
            timeout_seconds=timeout_seconds,
            passthrough=passthrough,
        )
        suite_reports.append(report)
        print(
            f"[collected] {suite.name}: ids={report['collected']} "
            f"errors={len(report['collection_errors'])} "
            f"exit={report['exit_code']} duration={report['duration_seconds']}s",
            flush=True,
        )
        if report["exit_code"] != 0 and not keep_going:
            break

    failed = [report["suite"] for report in suite_reports if report["exit_code"] != 0]
    recorder_failures = [
        report["suite"] for report in suite_reports if report["recorder_error"] is not None
    ]
    total_collected = sum(int(report["collected"]) for report in suite_reports)
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "audited_base_sha": manifest.audited_base_sha,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "environment": {
            "TZ": environment["TZ"],
            "PYTHONHASHSEED": environment["PYTHONHASHSEED"],
        },
        "status": "failed" if failed or recorder_failures else "passed",
        "selected_suites": [suite.name for suite in suites],
        "executed_suites": [report["suite"] for report in suite_reports],
        "failed_suites": failed,
        "recorder_failure_suites": recorder_failures,
        "total_collected": total_collected,
        "suites": suite_reports,
    }
    return payload, 1 if failed or recorder_failures else 0


def _load_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError(f"unsupported collection report: {path}")
    return payload


def _suite_node_ids(report: dict[str, Any]) -> dict[str, set[str]]:
    suites = report.get("suites")
    if not isinstance(suites, list):
        raise ValueError("collection report suites must be an array")
    result: dict[str, set[str]] = {}
    for suite in suites:
        if not isinstance(suite, dict):
            raise ValueError("collection report suite must be an object")
        name = suite.get("suite")
        node_ids = suite.get("node_ids")
        if not isinstance(name, str) or not isinstance(node_ids, list):
            raise ValueError("collection report suite has invalid node IDs")
        if name in result:
            raise ValueError(f"collection report contains duplicate suite names: {name}")
        if not all(isinstance(node_id, str) for node_id in node_ids):
            raise ValueError("collection report node ID must be a string")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError(f"collection report suite contains duplicate node IDs: {name}")
        result[name] = set(node_ids)
    return result


def _validate_relocation_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "::" in value:
        raise ValueError(f"{field} must be a relative POSIX Python test path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.suffix != ".py"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field} must be a relative POSIX Python test path")
    return path.as_posix()


def load_relocation_manifest(path: Path) -> NodeIdRelocationManifest:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if payload.get("schema_version") != 1:
        raise ValueError(f"unsupported node-id relocation manifest: {path}")
    audited_base_sha = payload.get("audited_base_sha")
    if not isinstance(audited_base_sha, str) or _GIT_SHA.fullmatch(audited_base_sha) is None:
        raise ValueError("relocation audited_base_sha must be a lowercase 40-character Git SHA")
    raw = payload.get("node_id_relocations")
    if not isinstance(raw, dict):
        raise ValueError("node_id_relocations must be a TOML table")
    relocations: dict[str, str] = {}
    targets: set[str] = set()
    for source_value, target_value in raw.items():
        source = _validate_relocation_path(source_value, "relocation source")
        target = _validate_relocation_path(target_value, f"relocation target for {source}")
        if source == target:
            raise ValueError(f"relocation source and target must differ: {source}")
        if target in targets:
            raise ValueError(f"relocation targets must be unique: {target}")
        relocations[source] = target
        targets.add(target)
    raw_migrations = payload.get("node_id_parameter_migrations", {})
    if not isinstance(raw_migrations, dict):
        raise ValueError("node_id_parameter_migrations must be a TOML table")
    parameter_id_migrations: dict[str, str] = {}
    migration_targets: set[str] = set()
    for source_value, target_value in raw_migrations.items():
        source = _validate_parameter_node_id(source_value, "parameter migration source")
        target = _validate_parameter_node_id(
            target_value, f"parameter migration target for {source}"
        )
        source_path, source_identity, source_case = source
        target_path, target_identity, target_case = target
        source_id = str(source_value)
        target_id = str(target_value)
        if source_path != target_path or source_identity != target_identity:
            raise ValueError("parameter-ID migrations may change only the final parameter ID")
        if source_case == target_case:
            raise ValueError(
                f"parameter-ID migration must change the final parameter ID: {source_id}"
            )
        if target_id in migration_targets:
            raise ValueError(f"parameter-ID migration targets must be unique: {target_id}")
        parameter_id_migrations[source_id] = target_id
        migration_targets.add(target_id)
    if set(parameter_id_migrations) & migration_targets:
        raise ValueError("parameter-ID migration sources and targets must not overlap")
    relocation_targets = set(relocations.values())
    for source_id, target_id in parameter_id_migrations.items():
        source_path = source_id.partition("::")[0]
        target_path = target_id.partition("::")[0]
        if source_path in relocations or target_path in relocation_targets:
            raise ValueError("parameter-ID migrations cannot overlap file relocations")
    return NodeIdRelocationManifest(
        audited_base_sha=audited_base_sha,
        relocations=relocations,
        parameter_id_migrations=parameter_id_migrations,
    )


def _validate_parameter_node_id(value: object, field: str) -> tuple[str, str, str]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a parameterized pytest node ID")
    path, separator, suffix = value.partition("::")
    if not separator:
        raise ValueError(f"{field} must be a parameterized pytest node ID")
    normalized_path = _validate_relocation_path(path, field)
    if path != normalized_path:
        raise ValueError(f"{field} must use a canonical relative POSIX test path")
    identity, bracket, case = suffix.rpartition("[")
    if not bracket or not case.endswith("]") or not identity or not case[:-1]:
        raise ValueError(f"{field} must have a final non-empty parameter ID")
    return normalized_path, identity, case[:-1]


def _split_node_id(node_id: str) -> tuple[str, str] | None:
    path, separator, suffix = node_id.partition("::")
    if not separator or not suffix:
        return None
    return path, suffix


def compare_reports(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    relocation_manifest: NodeIdRelocationManifest | None = None,
    require_exact: bool = False,
) -> dict[str, Any]:
    if relocation_manifest is not None:
        before_sha = before.get("audited_base_sha")
        after_sha = after.get("audited_base_sha")
        expected_sha = relocation_manifest.audited_base_sha
        if before_sha != expected_sha or after_sha != expected_sha:
            raise ValueError(
                "node-id relocation manifest audited base does not match collection reports"
            )
        relocations = dict(relocation_manifest.relocations)
        parameter_migrations = (
            {} if require_exact else dict(relocation_manifest.parameter_id_migrations)
        )
    else:
        relocations = {}
        parameter_migrations = {}

    before_suites = _suite_node_ids(before)
    after_suites = _suite_node_ids(after)
    suite_names = sorted(set(before_suites) | set(after_suites))
    suites: list[dict[str, Any]] = []
    total_removed = 0
    total_added = 0
    total_relocated = 0
    total_parameter_id_migrations = 0
    used_relocation_sources: set[str] = set()
    expected_relocation_sources: set[str] = set()
    parameter_migration_sources: set[str] = set()
    parameter_migration_source_suites: dict[str, str] = {}
    parameter_migration_target_suites: dict[str, str] = {}
    for suite_name, node_ids in before_suites.items():
        for source_id in parameter_migrations:
            if source_id in node_ids:
                if source_id in parameter_migration_source_suites:
                    raise ValueError(f"parameter-ID migration source is ambiguous: {source_id}")
                parameter_migration_source_suites[source_id] = suite_name
    for suite_name, node_ids in after_suites.items():
        for target_id in parameter_migrations.values():
            if target_id in node_ids:
                if target_id in parameter_migration_target_suites:
                    raise ValueError(f"parameter-ID migration target is ambiguous: {target_id}")
                parameter_migration_target_suites[target_id] = suite_name

    missing_migration_sources = sorted(
        set(parameter_migrations) - set(parameter_migration_source_suites)
    )
    if missing_migration_sources:
        raise ValueError(
            "declared parameter-ID migration source is missing from the before report: "
            + ", ".join(missing_migration_sources)
        )

    for source_id, suite_name in parameter_migration_source_suites.items():
        target_id = parameter_migrations[source_id]
        if any(target_id in node_ids for node_ids in before_suites.values()):
            raise ValueError(f"parameter-ID migration target already existed: {target_id}")
        if any(source_id in node_ids for node_ids in after_suites.values()):
            raise ValueError(f"parameter-ID migration source remains on head: {source_id}")
        if parameter_migration_target_suites.get(target_id) != suite_name:
            raise ValueError(
                f"parameter-ID migration target must be additive in the same suite: {target_id}"
            )

    for name in suite_names:
        before_ids = before_suites.get(name, set())
        after_ids = after_suites.get(name, set())
        raw_removed = before_ids - after_ids
        raw_added = after_ids - before_ids

        for node_id in before_ids:
            split = _split_node_id(node_id)
            if split is not None and split[0] in relocations:
                expected_relocation_sources.add(split[0])

        relocated: list[dict[str, str]] = []
        migrated_parameter_ids: list[dict[str, str]] = []
        relocated_sources: set[str] = set()
        relocated_targets: set[str] = set()
        for source_id in sorted(raw_removed):
            split = _split_node_id(source_id)
            if split is None:
                continue
            source_path, suffix = split
            target_path = relocations.get(source_path)
            if target_path is None:
                continue
            target_id = f"{target_path}::{suffix}"
            if target_id not in raw_added:
                continue
            if target_id in relocated_targets:
                raise ValueError(f"multiple historical node IDs map to target {target_id}")
            relocated.append({"from": source_id, "to": target_id})
            relocated_sources.add(source_id)
            relocated_targets.add(target_id)
            used_relocation_sources.add(source_path)

        for source_id in sorted(raw_removed):
            target_id = parameter_migrations.get(source_id)
            if target_id is None:
                continue
            if target_id not in raw_added:
                raise ValueError(f"parameter-ID migration target is not additive: {target_id}")
            migrated_parameter_ids.append({"from": source_id, "to": target_id})
            parameter_migration_sources.add(source_id)

        migrated_sources = {item["from"] for item in migrated_parameter_ids}
        migrated_targets = {item["to"] for item in migrated_parameter_ids}
        removed = sorted(raw_removed - relocated_sources - migrated_sources)
        added = sorted(raw_added - relocated_targets - migrated_targets)
        total_removed += len(removed)
        total_added += len(added)
        total_relocated += len(relocated)
        total_parameter_id_migrations += len(migrated_parameter_ids)
        suites.append(
            {
                "suite": name,
                "before": len(before_ids),
                "after": len(after_ids),
                "removed": removed,
                "added": added,
                "relocated": relocated,
                "parameter_id_migrations": migrated_parameter_ids,
            }
        )

    unused_required = sorted(expected_relocation_sources - used_relocation_sources)
    if unused_required:
        raise ValueError(
            "declared node-id relocation did not preserve any historical node IDs: "
            + ", ".join(unused_required)
        )

    unused_parameter_migrations = sorted(
        set(parameter_migration_source_suites) - parameter_migration_sources
    )
    if unused_parameter_migrations:
        raise ValueError(
            "declared parameter-ID migration did not preserve its historical node ID: "
            + ", ".join(unused_parameter_migrations)
        )

    exact_match = (
        total_removed == 0
        and total_added == 0
        and total_relocated == 0
        and total_parameter_id_migrations == 0
    )
    if total_removed:
        status = "removed-test-ids"
    elif total_added:
        status = "added-test-ids"
    elif total_relocated:
        status = "relocated-test-ids"
    elif total_parameter_id_migrations:
        status = "parameter-id-migrations"
    else:
        status = "exact-match"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "exact_match": exact_match,
        "before_audited_base_sha": before.get("audited_base_sha"),
        "after_audited_base_sha": after.get("audited_base_sha"),
        "relocation_manifest_audited_base_sha": (
            relocation_manifest.audited_base_sha if relocation_manifest is not None else None
        ),
        "total_removed": total_removed,
        "total_added": total_added,
        "total_relocated": total_relocated,
        "total_parameter_id_migrations": total_parameter_id_migrations,
        "suites": suites,
    }


def _print_change_ids(comparison: dict[str, Any]) -> None:
    removed_total = int(comparison.get("total_removed", 0))
    relocated_total = int(comparison.get("total_relocated", 0))
    parameter_migration_total = int(comparison.get("total_parameter_id_migrations", 0))
    if removed_total:
        print(f"[audited-base-removals] total={removed_total}", file=sys.stderr)
    if relocated_total:
        print(f"[audited-base-relocations] total={relocated_total}", file=sys.stderr)
    if parameter_migration_total:
        print(
            f"[audited-base-parameter-id-migrations] total={parameter_migration_total}",
            file=sys.stderr,
        )
    suites = comparison.get("suites")
    if not isinstance(suites, list):
        return
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        removed = suite.get("removed")
        if isinstance(removed, list) and removed:
            print(f"[removed-suite] {suite.get('suite')} count={len(removed)}", file=sys.stderr)
            for node_id in removed:
                if isinstance(node_id, str):
                    print(f"[removed-node-id] {node_id}", file=sys.stderr)
        relocated = suite.get("relocated")
        if isinstance(relocated, list) and relocated:
            print(
                f"[relocated-suite] {suite.get('suite')} count={len(relocated)}",
                file=sys.stderr,
            )
            for item in relocated:
                if isinstance(item, dict):
                    source = item.get("from")
                    target = item.get("to")
                    if isinstance(source, str) and isinstance(target, str):
                        print(f"[relocated-node-id] {source} -> {target}", file=sys.stderr)
        migrations = suite.get("parameter_id_migrations")
        if isinstance(migrations, list) and migrations:
            print(
                f"[parameter-id-migration-suite] {suite.get('suite')} count={len(migrations)}",
                file=sys.stderr,
            )
            for item in migrations:
                if isinstance(item, dict):
                    source = item.get("from")
                    target = item.get("to")
                    if isinstance(source, str) and isinstance(target, str):
                        print(f"[parameter-id-migration] {source} -> {target}", file=sys.stderr)


def split_pytest_args(argv: Sequence[str] | None) -> tuple[list[str], tuple[str, ...]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        return raw, ()
    separator = raw.index("--")
    return raw[:separator], tuple(raw[separator + 1 :])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("selection", nargs="+", help="suite or group names")
    collect.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--timeout-seconds", type=float, default=900.0)
    collect.add_argument("--keep-going", action="store_true")

    compare = subparsers.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--require-exact", action="store_true")
    compare.add_argument("--relocations", type=Path, default=DEFAULT_RELOCATION_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser_argv, passthrough = split_pytest_args(argv)
    parser = build_parser()
    args = parser.parse_args(parser_argv)
    try:
        if args.command == "collect":
            report, exit_code = build_collection_report(
                args.selection,
                manifest_path=args.manifest,
                timeout_seconds=args.timeout_seconds,
                keep_going=args.keep_going,
                passthrough=passthrough,
            )
            _atomic_write_json(args.output, report)
            return exit_code

        if passthrough:
            raise ValueError("pytest passthrough arguments are only valid for collect")
        before = _load_report(args.before)
        after = _load_report(args.after)
        relocation_manifest = load_relocation_manifest(args.relocations)
        comparison = compare_reports(
            before,
            after,
            relocation_manifest=relocation_manifest,
            require_exact=args.require_exact,
        )
        if args.output:
            _atomic_write_json(args.output, comparison)
        else:
            json.dump(comparison, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        _print_change_ids(comparison)
        if comparison["total_removed"]:
            return 1
        if args.require_exact and (
            comparison["total_added"]
            or comparison["total_relocated"]
            or comparison["total_parameter_id_migrations"]
        ):
            return 1
        return 0
    except (
        ManifestError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        print(f"collection baseline error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
