#!/usr/bin/env python3
"""Project the current test-suite manifest onto an older audited tree."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _project_entries(entries: object, base_root: Path) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    projected: list[dict[str, Any]] = []
    resolved_root = base_root.resolve()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("manifest entries must be tables")
        working_directory = raw_entry.get("working_directory")
        paths = raw_entry.get("paths")
        if not isinstance(working_directory, str) or not isinstance(paths, list):
            raise ValueError("manifest entry is missing working_directory or paths")
        cwd = (resolved_root / working_directory).resolve()
        try:
            cwd.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("working_directory escapes audited base") from exc
        if not cwd.is_dir():
            continue
        existing_paths: list[str] = []
        for raw_path in paths:
            if not isinstance(raw_path, str):
                raise ValueError("manifest entry path must be a string")
            target = (cwd / raw_path).resolve()
            try:
                target.relative_to(cwd)
            except ValueError as exc:
                raise ValueError("manifest path escapes suite working directory") from exc
            if target.exists():
                existing_paths.append(raw_path)
        if not existing_paths:
            continue
        entry = dict(raw_entry)
        entry["paths"] = existing_paths
        projected.append(entry)
    return projected


def project_manifest(raw: dict[str, Any], base_root: Path) -> dict[str, Any]:
    suites = _project_entries(raw.get("suites"), base_root)
    profiles = _project_entries(raw.get("profiles"), base_root)
    kept = {entry["name"] for entry in [*suites, *profiles] if isinstance(entry.get("name"), str)}
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        raise ValueError("manifest groups table is required")
    projected_groups: dict[str, list[str]] = {}
    for name, members in groups.items():
        if not isinstance(name, str) or not isinstance(members, list):
            raise ValueError("manifest group must contain a string array")
        projected_groups[name] = [
            member for member in members if isinstance(member, str) and member in kept
        ]
    return {
        "schema_version": raw.get("schema_version"),
        "audited_base_sha": raw.get("audited_base_sha"),
        "environment": raw.get("environment"),
        "groups": projected_groups,
        "suites": suites,
        "profiles": profiles,
    }


def _write_entry(handle: Any, entry: dict[str, Any], table_name: str) -> None:
    handle.write(f"[[{table_name}]]\n")
    ordered = (
        "name",
        "component",
        "working_directory",
        "paths",
        "pytest_args",
        "required_imports",
        "install_hint",
        "runtime_class",
        "ci_frequency",
        "categories",
        "external_dependencies",
    )
    for key in ordered:
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, str):
            handle.write(f"{key} = {_toml_string(value)}\n")
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            handle.write(f"{key} = {_toml_array(value)}\n")
        else:
            raise ValueError(f"unsupported manifest value for {table_name}.{key}")
    handle.write("\n")


def write_manifest(projected: dict[str, Any], output: Path) -> None:
    environment = projected.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("manifest environment table is required")
    schema_version = projected.get("schema_version")
    audited_base_sha = projected.get("audited_base_sha")
    if schema_version != 1 or not isinstance(audited_base_sha, str):
        raise ValueError("unsupported manifest identity")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"schema_version = {schema_version}\n")
        handle.write(f"audited_base_sha = {_toml_string(audited_base_sha)}\n\n")
        handle.write("[environment]\n")
        for key in ("timezone", "pythonhashseed"):
            value = environment.get(key)
            if not isinstance(value, str):
                raise ValueError(f"environment.{key} must be a string")
            handle.write(f"{key} = {_toml_string(value)}\n")
        handle.write("\n[groups]\n")
        groups = projected.get("groups")
        if not isinstance(groups, dict):
            raise ValueError("manifest groups table is required")
        for name, members in groups.items():
            if not isinstance(name, str) or not isinstance(members, list):
                raise ValueError("invalid projected group")
            handle.write(f"{name} = {_toml_array(members)}\n")
        handle.write("\n")
        for entry in projected.get("suites", []):
            _write_entry(handle, entry, "suites")
        for entry in projected.get("profiles", []):
            _write_entry(handle, entry, "profiles")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with args.source.open("rb") as handle:
        raw = tomllib.load(handle)
    projected = project_manifest(raw, args.base_root.resolve())
    write_manifest(projected, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
