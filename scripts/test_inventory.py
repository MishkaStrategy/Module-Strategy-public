#!/usr/bin/env python3
"""Build a static, machine-readable inventory of monorepo pytest files."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from test_suites import DEFAULT_MANIFEST, Manifest, Suite, load_manifest

ROOT = Path(__file__).resolve().parents[1]
ALWAYS_GENERATED_DIRECTORY_NAMES = {
    ".eggs",
    ".git",
    ".mypy_cache",
    ".nox",
    ".pip-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".sdist-venv",
    ".tox",
    ".venv",
    ".wheel-venv",
    "__pycache__",
    "dist-packages",
    "node_modules",
    "site-packages",
}
UNTRACKED_GENERATED_DIRECTORY_NAMES = {
    "artifacts",
    "build",
    "dist",
    "htmlcov",
    "reports",
    "wheelhouse",
}
GENERATED_DIRECTORY_PREFIXES = (
    ".pip-cache-",
    ".uv-cache-",
    "pip-build-env-",
    "pip-ephem-wheel-cache-",
    "pip-wheel-",
    "uv-cache-",
)
GENERATED_CACHE_PATH_PAIRS = {
    (".cache", "pip"),
    (".cache", "uv"),
}
NON_TEST_TOOLING_ROOTS = {"scripts"}
NON_CLASSIFICATION_MARKERS = {
    "filterwarnings",
    "parametrize",
    "skip",
    "skipif",
    "usefixtures",
    "xfail",
}
DEPENDENCY_IMPORTS = {
    "aiohttp",
    "fastapi",
    "httpx",
    "hypothesis",
    "numpy",
    "pandas",
    "pyarrow",
    "pytest_asyncio",
    "sqlalchemy",
}
NETWORK_IMPORTS = {"aiohttp", "httpx", "requests", "socket", "urllib"}
FILESYSTEM_IMPORTS = {"pathlib", "shutil", "tempfile"}
RANDOM_IMPORTS = {"random", "secrets", "uuid"}
SUBPROCESS_IMPORTS = {"multiprocessing", "subprocess"}
TIME_IMPORTS = {"datetime", "time"}
UNMAPPED_SUITE = Suite(
    name="unmapped",
    component="Unmapped test-like Python file",
    working_directory=Path("."),
    paths=(".",),
    required_imports=("pytest",),
    install_hint="map this file in config/test_suites.toml",
    runtime_class="unknown",
    ci_frequency="unknown",
    categories=("unknown",),
    external_dependencies=(),
)


@dataclass(frozen=True)
class FileInventory:
    path: str
    suite: str
    component: str
    module_basename: str
    static_test_functions: int
    fixtures: tuple[str, ...]
    markers: tuple[str, ...]
    parametrized_decorators: int
    imports: tuple[str, ...]
    dependency_imports: tuple[str, ...]
    uses_network_modules: bool
    uses_filesystem_modules: bool
    uses_random_modules: bool
    uses_subprocess_modules: bool
    uses_time_modules: bool
    uses_environment: bool
    async_tests: int
    parse_error: str | None


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[str]:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _dotted_name(target)
        if name:
            yield name


def _classification_marker(name: str) -> str | None:
    if not name.startswith("pytest.mark."):
        return None
    marker = name.removeprefix("pytest.mark.").split(".", 1)[0]
    return None if marker in NON_CLASSIFICATION_MARKERS else marker


def _module_markers(tree: ast.Module) -> set[str]:
    markers: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
        ):
            continue
        value = node.value
        candidates = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for candidate in candidates:
            target = candidate.func if isinstance(candidate, ast.Call) else candidate
            name = _dotted_name(target)
            if name:
                marker = _classification_marker(name)
                if marker:
                    markers.add(marker)
    return markers


def _uses_environment(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = _dotted_name(node)
            if name in {"os.environ", "os.getenv", "monkeypatch.setenv", "monkeypatch.delenv"}:
                return True
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in {"os.getenv", "monkeypatch.setenv", "monkeypatch.delenv"}:
                return True
    return False


def analyze_file(path: Path, suite: Suite) -> FileInventory:
    relative = path.relative_to(ROOT).as_posix()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
    except (OSError, SyntaxError, UnicodeError) as exc:
        return FileInventory(
            path=relative,
            suite=suite.name,
            component=suite.component,
            module_basename=path.name,
            static_test_functions=0,
            fixtures=(),
            markers=(),
            parametrized_decorators=0,
            imports=(),
            dependency_imports=(),
            uses_network_modules=False,
            uses_filesystem_modules=False,
            uses_random_modules=False,
            uses_subprocess_modules=False,
            uses_time_modules=False,
            uses_environment=False,
            async_tests=0,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    imports: set[str] = set()
    fixtures: set[str] = set()
    markers = _module_markers(tree)
    test_functions = 0
    async_tests = 0
    parametrized = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = tuple(_decorators(node))
            if node.name.startswith("test_"):
                test_functions += 1
                if isinstance(node, ast.AsyncFunctionDef):
                    async_tests += 1
            if any(name.endswith("fixture") for name in decorators):
                fixtures.add(node.name)
            for name in decorators:
                marker = _classification_marker(name)
                if marker:
                    markers.add(marker)
                if name == "pytest.mark.parametrize":
                    parametrized += 1

    dependency_imports = tuple(sorted(imports.intersection(DEPENDENCY_IMPORTS)))
    return FileInventory(
        path=relative,
        suite=suite.name,
        component=suite.component,
        module_basename=path.name,
        static_test_functions=test_functions,
        fixtures=tuple(sorted(fixtures)),
        markers=tuple(sorted(markers)),
        parametrized_decorators=parametrized,
        imports=tuple(sorted(imports)),
        dependency_imports=dependency_imports,
        uses_network_modules=bool(imports.intersection(NETWORK_IMPORTS)),
        uses_filesystem_modules=bool(imports.intersection(FILESYSTEM_IMPORTS)),
        uses_random_modules=bool(imports.intersection(RANDOM_IMPORTS)),
        uses_subprocess_modules=bool(imports.intersection(SUBPROCESS_IMPORTS)),
        uses_time_modules=bool(imports.intersection(TIME_IMPORTS)),
        uses_environment=_uses_environment(tree),
        async_tests=async_tests,
        parse_error=None,
    )


def suite_roots(manifest: Manifest) -> list[tuple[Path, Suite]]:
    roots: list[tuple[Path, Suite]] = []
    for suite in manifest.suites.values():
        cwd = ROOT / suite.working_directory
        for relative in suite.paths:
            roots.append(((cwd / relative).resolve(), suite))
    roots.sort(key=lambda item: len(item[0].parts), reverse=True)
    return roots


def _decode_git_paths(payload: bytes) -> tuple[Path, ...]:
    root = ROOT.resolve()
    paths: list[Path] = []
    for raw_path in payload.split(b"\0"):
        if not raw_path:
            continue
        candidate = (root / os.fsdecode(raw_path)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            paths.append(candidate)
    return tuple(paths)


def _git_python_files() -> tuple[tuple[Path, bool], ...] | None:
    commands = (
        (True, ("--cached",)),
        (False, ("--others", "--exclude-standard")),
    )
    discovered: dict[Path, bool] = {}
    for tracked, selection in commands:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", *selection, "--", "*.py"],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            return None
        for path in _decode_git_paths(completed.stdout):
            discovered[path] = discovered.get(path, False) or tracked
    return tuple(sorted(discovered.items(), key=lambda item: item[0].as_posix()))


@lru_cache(maxsize=None)
def _has_pyvenv_marker(directory: Path) -> bool:
    return (directory / "pyvenv.cfg").is_file()


def _is_generated_python_path(path: Path, *, tracked: bool) -> bool:
    root = ROOT.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return True

    directory_parts = relative.parts[:-1]
    if any(part in ALWAYS_GENERATED_DIRECTORY_NAMES for part in directory_parts):
        return True
    if any(
        part.startswith(prefix)
        for part in directory_parts
        for prefix in GENERATED_DIRECTORY_PREFIXES
    ):
        return True
    if any(
        pair in GENERATED_CACHE_PATH_PAIRS for pair in zip(directory_parts, directory_parts[1:])
    ):
        return True
    if not tracked and any(part in UNTRACKED_GENERATED_DIRECTORY_NAMES for part in directory_parts):
        return True

    current = resolved.parent
    while current != root:
        if _has_pyvenv_marker(current):
            return True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _python_file_candidates() -> tuple[tuple[Path, bool], ...]:
    git_files = _git_python_files()
    if git_files is not None:
        return git_files
    return tuple((path.resolve(), False) for path in ROOT.rglob("*.py") if path.is_file())


def _discover_python_files(predicate: Callable[[Path], bool]) -> tuple[Path, ...]:
    files: list[Path] = []
    for path, tracked in _python_file_candidates():
        if _is_generated_python_path(path, tracked=tracked):
            continue
        if predicate(path):
            files.append(path.resolve())
    return tuple(sorted(set(files)))


def discover_test_files() -> tuple[Path, ...]:
    return _discover_python_files(
        lambda path: path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def discover_fixture_files() -> tuple[Path, ...]:
    return _discover_python_files(lambda path: path.name == "conftest.py")


def map_suite(path: Path, roots: Sequence[tuple[Path, Suite]]) -> Suite | None:
    matches: list[Suite] = []
    for root, suite in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        matches.append(suite)
    unique = {suite.name: suite for suite in matches}
    if len(unique) > 1:
        names = ", ".join(sorted(unique))
        raise RuntimeError(f"test file {path} maps to multiple suites: {names}")
    return next(iter(unique.values()), None)


def _is_non_test_tooling_file(path: Path, inventory: FileInventory) -> bool:
    relative = path.relative_to(ROOT)
    return (
        bool(relative.parts)
        and relative.parts[0] in NON_TEST_TOOLING_ROOTS
        and inventory.static_test_functions == 0
        and not inventory.fixtures
        and not inventory.markers
        and inventory.parse_error is None
    )


def _analyze_mapped_files(
    paths: Sequence[Path], roots: Sequence[tuple[Path, Suite]]
) -> tuple[list[FileInventory], list[str]]:
    inventories: list[FileInventory] = []
    unmapped: list[str] = []
    for path in paths:
        suite = map_suite(path, roots)
        if suite is None:
            unmapped.append(path.relative_to(ROOT).as_posix())
            continue
        inventories.append(analyze_file(path, suite))
    return inventories, unmapped


def build_inventory(manifest: Manifest) -> dict[str, Any]:
    roots = suite_roots(manifest)
    test_paths = discover_test_files()
    fixture_paths = discover_fixture_files()
    unmapped_tests: list[str] = []
    test_named_non_test_files: list[str] = []
    test_inventories: list[FileInventory] = []
    for path in test_paths:
        suite = map_suite(path, roots)
        if suite is None:
            candidate = analyze_file(path, UNMAPPED_SUITE)
            if _is_non_test_tooling_file(path, candidate):
                test_named_non_test_files.append(candidate.path)
                continue
            unmapped_tests.append(candidate.path)
            continue
        test_inventories.append(analyze_file(path, suite))

    fixture_inventories, unmapped_fixtures = _analyze_mapped_files(fixture_paths, roots)
    all_inventories = [*test_inventories, *fixture_inventories]

    basename_paths: dict[str, list[str]] = defaultdict(list)
    for item in test_inventories:
        basename_paths[item.module_basename].append(item.path)
    duplicates = {
        basename: sorted(paths)
        for basename, paths in sorted(basename_paths.items())
        if len(paths) > 1
    }
    suite_counts = Counter(item.suite for item in test_inventories)
    fixture_suite_counts = Counter(item.suite for item in fixture_inventories)
    marker_counts = Counter(marker for item in all_inventories for marker in item.markers)
    dependency_counts = Counter(
        dependency for item in all_inventories for dependency in item.dependency_imports
    )
    parse_errors = [item.path for item in all_inventories if item.parse_error]
    return {
        "schema_version": 1,
        "audited_base_sha": manifest.audited_base_sha,
        "summary": {
            "discovered_test_named_python_files": len(test_paths),
            "test_files": len(test_inventories) + len(unmapped_tests),
            "mapped_test_files": len(test_inventories),
            "unmapped_test_files": len(unmapped_tests),
            "test_named_non_test_files": len(test_named_non_test_files),
            "fixture_files": len(fixture_paths),
            "mapped_fixture_files": len(fixture_inventories),
            "unmapped_fixture_files": len(unmapped_fixtures),
            "duplicate_basenames": len(duplicates),
            "static_test_functions": sum(item.static_test_functions for item in test_inventories),
            "fixture_definitions": sum(len(item.fixtures) for item in all_inventories),
            "parametrized_decorators": sum(
                item.parametrized_decorators for item in test_inventories
            ),
            "parse_errors": len(parse_errors),
        },
        "suite_file_counts": dict(sorted(suite_counts.items())),
        "fixture_suite_file_counts": dict(sorted(fixture_suite_counts.items())),
        "marker_file_counts": dict(sorted(marker_counts.items())),
        "dependency_file_counts": dict(sorted(dependency_counts.items())),
        "duplicate_module_basenames": duplicates,
        "unmapped_test_files": sorted(unmapped_tests),
        "unmapped_fixture_files": sorted(unmapped_fixtures),
        "test_named_non_test_files": sorted(test_named_non_test_files),
        "parse_error_files": sorted(parse_errors),
        "files": [asdict(item) for item in test_inventories],
        "fixture_files": [asdict(item) for item in fixture_inventories],
        "dynamic_fields_not_available_statically": [
            "collected_test_ids",
            "expanded_parametrized_cases",
            "skipped",
            "xfailed",
            "deselected",
            "collection_errors",
            "runtime_seconds",
            "ci_workflow_job",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--fail-on-unmapped", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        inventory = build_inventory(manifest)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"test inventory error: {exc}", file=sys.stderr)
        return 2
    indent = None if args.compact else 2
    payload = json.dumps(inventory, ensure_ascii=False, indent=indent, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    summary = inventory["summary"]
    if args.fail_on_unmapped and (
        summary["unmapped_test_files"] or summary["unmapped_fixture_files"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
