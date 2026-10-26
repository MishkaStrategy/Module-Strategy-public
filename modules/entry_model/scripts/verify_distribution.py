from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

_REQUIRED_SDIST_SUFFIXES = {
    "MANIFEST.in",
    "pyproject.toml",
    "tests/__init__.py",
    "tests/support.py",
    "tests/test_core_integration.py",
    "benchmarks/benchmark_entry_model.py",
    "scripts/clean_wheel_smoke.py",
    "scripts/verify_distribution.py",
    "examples/entry_model.example.yaml",
    "docs/ERROR_DISCOVERY_AUDIT.md",
}
_REQUIRED_WHEEL_SUFFIXES = {
    "modular_modules/entry_model/__init__.py",
    "modular_modules/entry_model/py.typed",
    "modular_strategy_entry_model-0.2.0.dist-info/entry_points.txt",
}


def _normalized_tar_members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
    return {name.split("/", 1)[1] if "/" in name else name for name in members}


def _normalized_wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return {name for name in archive.namelist() if not name.endswith("/")}


def _require(members: set[str], required: set[str], artifact: Path) -> None:
    missing = sorted(required - members)
    if missing:
        raise SystemExit(f"{artifact.name} is missing required files: {', '.join(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Entry Model distribution contents")
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    _require(_normalized_tar_members(args.sdist), _REQUIRED_SDIST_SUFFIXES, args.sdist)
    _require(_normalized_wheel_members(args.wheel), _REQUIRED_WHEEL_SUFFIXES, args.wheel)
    print({"sdist": "PASS", "wheel": "PASS"})


if __name__ == "__main__":
    main()
