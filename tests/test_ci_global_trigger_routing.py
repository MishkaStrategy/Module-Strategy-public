from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _event_paths(workflow: str, event: str) -> tuple[str, ...]:
    lines = (ROOT / workflow).read_text(encoding="utf-8").splitlines()
    marker = f"  {event}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"{workflow} is missing {event!r} trigger") from exc

    event_lines: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith("    "):
            break
        event_lines.append(line)

    try:
        paths_start = event_lines.index("    paths:")
    except ValueError as exc:
        raise AssertionError(f"{workflow} {event!r} trigger must be path-scoped, not bare") from exc

    paths: list[str] = []
    for line in event_lines[paths_start + 1 :]:
        if not line.startswith("      - "):
            break
        paths.append(line.removeprefix("      - ").strip().strip('"'))
    if not paths:
        raise AssertionError(f"{workflow} {event!r} trigger has an empty paths list")
    return tuple(paths)


@pytest.mark.parametrize(
    ("workflow", "own_workflow"),
    [
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        (".github/workflows/fast-checks.yml", ".github/workflows/fast-checks.yml"),
    ],
)
def test_global_automatic_ci_is_path_scoped(workflow: str, own_workflow: str) -> None:
    for event in ("push", "pull_request"):
        paths = _event_paths(workflow, event)
        assert own_workflow in paths
        assert ".github/workflows/**" not in paths


def test_core_ci_preserves_historical_focused_ownership() -> None:
    paths = set(_event_paths(".github/workflows/ci.yml", "pull_request"))

    assert {
        "src/modular_core/**",
        "src/strategy_modules/**",
        "tests/**",
        "scripts/**",
        "pyproject.toml",
        ".github/actions/python-runner-preflight/**",
        ".github/workflows/ci.yml",
    } <= paths
    assert "modules/**" not in paths
    assert "plugins/**" not in paths
    assert "applications/**" not in paths


def test_fast_checks_cover_python_product_surfaces_without_all_workflows() -> None:
    paths = set(_event_paths(".github/workflows/fast-checks.yml", "pull_request"))

    assert {
        "src/**",
        "tests/**",
        "scripts/**",
        "modules/**",
        "plugins/**",
        "applications/**",
        "research/**",
        "pyproject.toml",
        ".github/actions/**",
        ".github/workflows/fast-checks.yml",
    } <= paths
    assert ".github/workflows/**" not in paths
