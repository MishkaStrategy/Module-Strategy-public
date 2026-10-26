"""Report declared package inputs without resolving or evaluating them.

The report is an inventory of source declarations. It is not a resolved lock,
an installation plan, or evidence that either supported Python runtime can
reconstruct the declarations.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

from scripts.platform_release_manifest import (
    CANONICAL_REPOSITORY,
    ManifestError,
    _clean_exact_source,
    _canonical_v3_python_dependency_selection,
    _discover_distributions,
    _git,
    _read_path,
    _strict_lock_json,
    _tracked_paths,
    _validate_npm_lock_v3,
    canonical_json_bytes,
    sha256_bytes,
)

NPM_PACKAGE_PATH = "web/market-data-console/package.json"
NPM_LOCK_PATH = "web/market-data-console/package-lock.json"
REPORT_SCHEMA_VERSION = 1
SELECTION_FRAGMENT_KIND = "source_bound_profile_selection_fragment"


def _declared_field(project: dict[str, Any], field: str, dynamic: set[str]) -> dict[str, Any]:
    if field in dynamic:
        return {
            "state": "unsupported_dynamic",
            "dynamic_provider_required": True,
        }
    if field not in project:
        return {"state": "absent"}
    return {"state": "static", "value": project[field]}


def _python_input(name: str, version: str, descriptor_path: str, content: bytes) -> dict[str, Any]:
    try:
        project_data = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"cannot parse Python descriptor {descriptor_path}: {exc}") from exc
    project = project_data.get("project")
    if not isinstance(project, dict):
        raise ManifestError(f"Python descriptor {descriptor_path} lacks a project table")
    dynamic_raw = project.get("dynamic", [])
    if (
        not isinstance(dynamic_raw, list)
        or any(not isinstance(item, str) for item in dynamic_raw)
        or len(dynamic_raw) != len(set(dynamic_raw))
    ):
        raise ManifestError(f"Python descriptor {descriptor_path} has malformed project.dynamic")
    dynamic = set(dynamic_raw)

    build_system = project_data.get("build-system")
    if build_system is None:
        build_report: dict[str, Any] = {"state": "incomplete", "reason": "missing_build_system"}
    elif not isinstance(build_system, dict):
        build_report = {"state": "unsupported", "reason": "build_system_must_be_a_table"}
    else:
        requirements = build_system.get("requires")
        backend = build_system.get("build-backend")
        backend_path = build_system.get("backend-path")
        valid_shape = (
            isinstance(requirements, list)
            and all(isinstance(item, str) for item in requirements)
            and isinstance(backend, str)
            and bool(backend)
            and (
                backend_path is None
                or (
                    isinstance(backend_path, list)
                    and all(isinstance(item, str) for item in backend_path)
                )
            )
        )
        state = "declared" if valid_shape else "incomplete"
        build_report = {
            "state": state,
            "value": build_system,
            **({} if valid_shape else {"reason": "incomplete_or_unsupported_build_system_fields"}),
        }

    optional = _declared_field(project, "optional-dependencies", dynamic)
    if optional["state"] == "static":
        groups = optional["value"]
        if not isinstance(groups, dict) or any(
            not isinstance(group, str)
            or not isinstance(requirements, list)
            or any(not isinstance(requirement, str) for requirement in requirements)
            for group, requirements in groups.items()
        ):
            optional = {"state": "unsupported", "reason": "invalid_optional_dependency_groups"}
        else:
            # Preserve group names, requirement order, duplicates, markers, and
            # requested extras exactly as parsed from the static declaration.
            optional = {
                "state": "static",
                "groups": [
                    {"name": group, "requirements": requirements}
                    for group, requirements in groups.items()
                ],
            }

    return {
        "name": name,
        "version": version,
        "descriptor": {
            "path": descriptor_path,
            "sha256": sha256_bytes(content),
        },
        "requires_python": _declared_field(project, "requires-python", dynamic),
        "build_system": build_report,
        "dependencies": _declared_field(project, "dependencies", dynamic),
        "optional_dependencies": optional,
        "unsupported_dynamic_fields": sorted(
            dynamic & {"requires-python", "dependencies", "optional-dependencies"}
        ),
    }


def build_dependency_input_report(source_root: str | Path, source_commit: str) -> dict[str, Any]:
    """Generate a deterministic, source-bound inventory of declared inputs."""
    source = Path(source_root).resolve(strict=True)
    if not source.is_dir():
        raise ManifestError("source_root must be a directory")
    _clean_exact_source(source, source_commit)
    tracked = _tracked_paths(source)

    discovered = _discover_distributions(
        source,
        source,
        tracked,
        source_commit,
        allow_dynamic_requires_python=True,
    )
    python_inputs = []
    for name, (version, descriptor_path, content, _requires_python, _dependencies) in sorted(
        discovered.items(), key=lambda item: (item[1][1], item[0])
    ):
        python_inputs.append(_python_input(name, version, descriptor_path, content))

    package_bytes = _read_path(
        "source", NPM_PACKAGE_PATH, "npm package descriptor", source, source, tracked, source_commit
    )
    lock_bytes = _read_path(
        "source", NPM_LOCK_PATH, "npm package lock", source, source, tracked, source_commit
    )
    package = _strict_lock_json(package_bytes, "source npm package.json")
    # Reuse the release validator's source/lock consistency check. This checks
    # root declarations and lock structure; it does not resolve dependencies.
    _validate_npm_lock_v3(lock_bytes, package_bytes, "source npm package-lock.json")
    npm_fields = (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "peerDependenciesMeta",
        "engines",
        "os",
        "cpu",
        "libc",
        "packageManager",
    )
    npm_report = {
        "package": {"path": NPM_PACKAGE_PATH, "sha256": sha256_bytes(package_bytes)},
        "lock": {"path": NPM_LOCK_PATH, "sha256": sha256_bytes(lock_bytes)},
        "name": package.get("name"),
        "version": package.get("version"),
        "metadata": {
            field: {"state": "static", "value": package[field]}
            if field in package
            else {"state": "absent"}
            for field in npm_fields
        },
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": {
            "repository": CANONICAL_REPOSITORY,
            "commit": source_commit,
        },
        "python_distributions": python_inputs,
        "python_distribution_count": len(python_inputs),
        "npm_project": npm_report,
        "resolution_status": {
            "state": "declared_inputs_only",
            "resolved_locks": False,
            "python_312_313_reconstruction_proven": False,
        },
    }


def validate_dependency_input_report(report: Any, source_root: str | Path) -> None:
    """Require a report to exactly match declarations in the clean source HEAD."""
    if not isinstance(report, dict) or not isinstance(report.get("source"), dict):
        raise ManifestError("dependency input report must contain a source object")
    source = Path(source_root).resolve(strict=True)
    head = _git(source, "rev-parse", "HEAD")
    if report["source"].get("commit") != head:
        raise ManifestError("dependency input report source commit differs from repository HEAD")
    expected = build_dependency_input_report(source, head)
    if canonical_json_bytes(report) != canonical_json_bytes(expected):
        raise ManifestError("dependency input report differs from source declarations")


def build_profile_selection_fragment(source_root: str | Path, source_commit: str) -> dict[str, Any]:
    """Build a source-bound v3 profile selection fragment, never a resolver input."""
    source = Path(source_root).resolve(strict=True)
    if not source.is_dir():
        raise ManifestError("source_root must be a directory")
    _clean_exact_source(source, source_commit)
    tracked = _tracked_paths(source)
    discovered = _discover_distributions(source, source, tracked, source_commit)
    selection = _canonical_v3_python_dependency_selection(list(discovered))
    return {
        "artifact_type": SELECTION_FRAGMENT_KIND,
        "source": {"repository": CANONICAL_REPOSITORY, "commit": source_commit},
        "python_dependency_selection": selection,
        "usage": "v3 profile fragment only; not a resolver input, lock, receipt, or acceptance",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--selection-fragment",
        action="store_true",
        help="print the source-bound v3 profile selection fragment only",
    )
    args = parser.parse_args(argv)
    try:
        commit = args.source_commit or _git(args.source_root, "rev-parse", "HEAD")
        report = (
            build_profile_selection_fragment(args.source_root, commit)
            if args.selection_fragment
            else build_dependency_input_report(args.source_root, commit)
        )
        encoded = canonical_json_bytes(report) + b"\n"
        if args.output:
            args.output.write_bytes(encoded)
        else:
            sys.stdout.buffer.write(encoded)
    except ManifestError as exc:
        print(f"dependency input report failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
