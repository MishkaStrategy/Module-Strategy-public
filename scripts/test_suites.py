#!/usr/bin/env python3
"""Run monorepo pytest suites and non-owning profiles in isolated subprocesses."""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "test_suites.toml"


class ManifestError(RuntimeError):
    """Raised when the suite manifest is invalid."""


@dataclass(frozen=True)
class Suite:
    name: str
    component: str
    working_directory: Path
    paths: tuple[str, ...]
    required_imports: tuple[str, ...]
    install_hint: str
    runtime_class: str
    ci_frequency: str
    categories: tuple[str, ...]
    external_dependencies: tuple[str, ...]
    pytest_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    audited_base_sha: str
    timezone: str
    pythonhashseed: str
    groups: dict[str, tuple[str, ...]]
    suites: dict[str, Suite]
    profiles: dict[str, Suite]


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ManifestError(f"{field} must be a string array")
    return tuple(value)


def _optional_string_tuple(value: object, field: str) -> tuple[str, ...]:
    return () if value is None else _string_tuple(value, field)


def _load_entries(value: object, field: str, *, required: bool) -> dict[str, Suite]:
    if value is None and not required:
        return {}
    if not isinstance(value, list) or (required and not value):
        requirement = "at least one entry is required" if required else "must be an array"
        raise ManifestError(f"{field}: {requirement}")
    entries: dict[str, Suite] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ManifestError(f"{field}[{index}] must be a table")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{field}[{index}].name must be a non-empty string")
        if name in entries:
            raise ManifestError(f"duplicate {field} name: {name}")
        component = item.get("component")
        working_directory = item.get("working_directory")
        install_hint = item.get("install_hint")
        runtime_class = item.get("runtime_class")
        ci_frequency = item.get("ci_frequency")
        scalar_fields = {
            "component": component,
            "working_directory": working_directory,
            "install_hint": install_hint,
            "runtime_class": runtime_class,
            "ci_frequency": ci_frequency,
        }
        for scalar_field, scalar_value in scalar_fields.items():
            if not isinstance(scalar_value, str) or not scalar_value:
                raise ManifestError(f"{field} {name}: {scalar_field} must be a non-empty string")
        entries[name] = Suite(
            name=name,
            component=component,
            working_directory=Path(working_directory),
            paths=_string_tuple(item.get("paths"), f"{field} {name}.paths"),
            required_imports=_string_tuple(
                item.get("required_imports"), f"{field} {name}.required_imports"
            ),
            install_hint=install_hint,
            runtime_class=runtime_class,
            ci_frequency=ci_frequency,
            categories=_string_tuple(item.get("categories"), f"{field} {name}.categories"),
            external_dependencies=_string_tuple(
                item.get("external_dependencies"),
                f"{field} {name}.external_dependencies",
            ),
            pytest_args=_optional_string_tuple(
                item.get("pytest_args"), f"{field} {name}.pytest_args"
            ),
        )
    return entries


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    if raw.get("schema_version") != 1:
        raise ManifestError("unsupported test suite manifest schema")
    audited_base_sha = raw.get("audited_base_sha")
    if not isinstance(audited_base_sha, str) or len(audited_base_sha) != 40:
        raise ManifestError("audited_base_sha must be a 40-character commit SHA")
    environment = raw.get("environment")
    if not isinstance(environment, dict):
        raise ManifestError("environment table is required")
    timezone = environment.get("timezone")
    pythonhashseed = environment.get("pythonhashseed")
    if not isinstance(timezone, str) or not timezone:
        raise ManifestError("environment.timezone must be a non-empty string")
    if not isinstance(pythonhashseed, str) or not pythonhashseed:
        raise ManifestError("environment.pythonhashseed must be a non-empty string")

    suites = _load_entries(raw.get("suites"), "suites", required=True)
    profiles = _load_entries(raw.get("profiles"), "profiles", required=False)
    duplicate_names = sorted(set(suites).intersection(profiles))
    if duplicate_names:
        raise ManifestError(f"suite/profile names overlap: {', '.join(duplicate_names)}")
    available = set(suites) | set(profiles)

    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise ManifestError("groups table is required")
    groups: dict[str, tuple[str, ...]] = {}
    for name, members in raw_groups.items():
        group = _string_tuple(members, f"group {name}")
        unknown = sorted(set(group).difference(available))
        if unknown:
            raise ManifestError(f"group {name} references unknown entries: {', '.join(unknown)}")
        groups[name] = group

    manifest = Manifest(
        schema_version=1,
        audited_base_sha=audited_base_sha,
        timezone=timezone,
        pythonhashseed=pythonhashseed,
        groups=groups,
        suites=suites,
        profiles=profiles,
    )
    validate_paths(manifest)
    return manifest


def validate_paths(manifest: Manifest) -> None:
    for suite in (*manifest.suites.values(), *manifest.profiles.values()):
        cwd = (ROOT / suite.working_directory).resolve()
        try:
            cwd.relative_to(ROOT)
        except ValueError as exc:
            raise ManifestError(
                f"suite {suite.name}: working directory escapes repository"
            ) from exc
        if not cwd.is_dir():
            raise ManifestError(f"suite {suite.name}: missing working directory {cwd}")
        for relative in suite.paths:
            target = (cwd / relative).resolve()
            try:
                target.relative_to(cwd)
            except ValueError as exc:
                raise ManifestError(f"suite {suite.name}: path escapes working directory") from exc
            if not target.exists():
                raise ManifestError(f"suite {suite.name}: missing path {target}")


def resolve_selection(manifest: Manifest, requested: Sequence[str]) -> tuple[Suite, ...]:
    entries = {**manifest.suites, **manifest.profiles}
    names: list[str] = []
    for token in requested:
        if token in manifest.groups:
            names.extend(manifest.groups[token])
        elif token in entries:
            names.append(token)
        else:
            choices = sorted(set(manifest.groups) | set(entries))
            raise ManifestError(
                f"unknown suite, profile or group {token!r}; choices: {', '.join(choices)}"
            )
    deduplicated = tuple(dict.fromkeys(names))
    if not deduplicated:
        raise ManifestError("no suites or profiles selected")
    return tuple(entries[name] for name in deduplicated)


def missing_imports(suite: Suite) -> tuple[str, ...]:
    missing: list[str] = []
    for name in suite.required_imports:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return tuple(missing)


def preflight(suites: Sequence[Suite]) -> bool:
    ok = True
    for suite in suites:
        missing = missing_imports(suite)
        if not missing:
            print(f"[dependency-ok] {suite.name}")
            continue
        ok = False
        print(
            f"[dependency-missing] {suite.name}: {', '.join(missing)}\n"
            f"  install: {suite.install_hint}",
            file=sys.stderr,
        )
    return ok


def command_for(suite: Suite, collect_only: bool, passthrough: Sequence[str]) -> list[str]:
    command = [sys.executable, "-m", "pytest"]
    if collect_only:
        command.append("--collect-only")
    command.extend(suite.pytest_args)
    command.extend(suite.paths)
    command.extend(passthrough)
    return command


def run_suites(
    manifest: Manifest,
    suites: Sequence[Suite],
    *,
    collect_only: bool,
    keep_going: bool,
    passthrough: Sequence[str],
) -> int:
    if not preflight(suites):
        return 2
    environment = os.environ.copy()
    environment.setdefault("TZ", manifest.timezone)
    environment.setdefault("PYTHONHASHSEED", manifest.pythonhashseed)
    failed: list[tuple[str, int]] = []
    for suite in suites:
        cwd = ROOT / suite.working_directory
        command = command_for(suite, collect_only, passthrough)
        print(f"[suite] {suite.name} ({suite.component})")
        print(f"[cwd] {cwd}")
        print(f"[command] {' '.join(command)}")
        completed = subprocess.run(command, cwd=cwd, env=environment, check=False)
        print(f"[exit] {suite.name}: {completed.returncode}")
        if completed.returncode != 0:
            failed.append((suite.name, completed.returncode))
            if not keep_going:
                break
    if failed:
        summary = ", ".join(f"{name}={code}" for name, code in failed)
        print(f"[failed] {summary}", file=sys.stderr)
        return 1
    return 0


def print_manifest(manifest: Manifest) -> None:
    print(f"schema={manifest.schema_version} audited_base={manifest.audited_base_sha}")
    for kind, entries in (("suite", manifest.suites), ("profile", manifest.profiles)):
        for suite in entries.values():
            categories = ",".join(suite.categories)
            pytest_args = ",".join(suite.pytest_args) or "-"
            print(
                f"{suite.name}: kind={kind} runtime={suite.runtime_class} "
                f"frequency={suite.ci_frequency} categories={categories} "
                f"cwd={suite.working_directory} paths={','.join(suite.paths)} "
                f"pytest_args={pytest_args}"
            )
    for name, members in manifest.groups.items():
        print(f"group {name}: {','.join(members)}")


def split_pytest_args(argv: Sequence[str] | None) -> tuple[list[str], tuple[str, ...]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        return raw, ()
    separator = raw.index("--")
    return raw[:separator], tuple(raw[separator + 1 :])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="print suites, profiles and groups")
    for command in ("check", "collect", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("selection", nargs="+", help="suite, profile or group names")
        child.add_argument("--keep-going", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser_argv, passthrough = split_pytest_args(argv)
    parser = build_parser()
    args = parser.parse_args(parser_argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "list":
            if passthrough:
                raise ManifestError("pytest passthrough arguments are not valid for list")
            print_manifest(manifest)
            return 0
        suites = resolve_selection(manifest, args.selection)
        if args.command == "check":
            if passthrough:
                raise ManifestError("pytest passthrough arguments are not valid for check")
            return 0 if preflight(suites) else 2
        return run_suites(
            manifest,
            suites,
            collect_only=args.command == "collect",
            keep_going=args.keep_going,
            passthrough=passthrough,
        )
    except (ManifestError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"test suite configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
