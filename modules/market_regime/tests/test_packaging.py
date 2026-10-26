from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def test_sdist_contains_test_support_and_handoff_sources(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    isolated_project = tmp_path / "project"
    isolated_project.mkdir()
    for filename in ("README.md", "pyproject.toml", "MANIFEST.in"):
        shutil.copy2(project_root / filename, isolated_project / filename)
    for directory in ("src", "tests", "docs", "examples", "benchmarks"):
        shutil.copytree(project_root / directory, isolated_project / directory)

    output = tmp_path / "dist"
    output.mkdir()
    environment = os.environ.copy()
    environment["MARKET_REGIME_SDIST_OUT"] = str(output)
    script = (
        "import os; "
        "from setuptools import build_meta; "
        "build_meta.build_sdist(os.environ['MARKET_REGIME_SDIST_OUT'])"
    )
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=isolated_project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    archive = next(output.glob("*.tar.gz"))
    with tarfile.open(archive, "r:gz") as source:
        members = {Path(name) for name in source.getnames()}

    root = Path(archive.name.removesuffix(".tar.gz"))
    required = {
        root / "MANIFEST.in",
        root / "tests" / "conftest.py",
        root / "tests" / "fixture_v1_real_checkpoint.json",
        root / "docs" / "STATE_SCHEMA_AND_MIGRATION.md",
        root / "examples" / "consumer.py",
        root / "benchmarks" / "benchmark.py",
    }
    assert required <= members
