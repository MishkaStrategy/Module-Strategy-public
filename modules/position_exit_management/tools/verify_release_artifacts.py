from __future__ import annotations

import argparse
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

FORBIDDEN_PARTS = {".git", ".venv", "__pycache__"}
FORBIDDEN_NAMES = {".env", ".env.local"}


def _validate_names(names: Iterable[str]) -> set[str]:
    normalized = {name.replace("\\", "/") for name in names}
    for name in normalized:
        parts = set(Path(name).parts)
        if parts & FORBIDDEN_PARTS or Path(name).name in FORBIDDEN_NAMES:
            raise ValueError(f"forbidden release member: {name}")
    return normalized


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = _validate_names(archive.namelist())
    required_suffixes = {
        "modular_strategy_position_exit_management/__init__.py",
        "modular_strategy_position_exit_management/module.py",
        "modular_strategy_position_exit_management/py.typed",
    }
    for suffix in required_suffixes:
        if not any(name.endswith(suffix) for name in names):
            raise ValueError(f"wheel missing required member: {suffix}")
    if any("/tests/" in f"/{name}" for name in names):
        raise ValueError("wheel must not contain test sources")


def verify_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = _validate_names(archive.getnames())
    required_suffixes = {
        "/pyproject.toml",
        "/MANIFEST.in",
        "/README.md",
        "/tests/conftest.py",
        "/benchmarks/benchmark.py",
        "/tools/verify_release_artifacts.py",
        "/tools/replay_evidence.py",
    }
    for suffix in required_suffixes:
        if not any(f"/{name}".endswith(suffix) for name in names):
            raise ValueError(f"sdist missing required member: {suffix}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    args = parser.parse_args()
    verify_wheel(args.wheel)
    verify_sdist(args.sdist)
    print({"wheel": str(args.wheel), "sdist": str(args.sdist), "verified": True})


if __name__ == "__main__":
    main()
