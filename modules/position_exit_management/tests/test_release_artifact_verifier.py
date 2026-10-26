from __future__ import annotations

import importlib.util
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "verify_release_artifacts.py"
SPEC = importlib.util.spec_from_file_location("position_exit_artifact_verifier", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)


def test_round_27_release_verifier_accepts_expected_layout(tmp_path: Path):
    wheel = tmp_path / "module.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in (
            "modular_strategy_position_exit_management/__init__.py",
            "modular_strategy_position_exit_management/module.py",
            "modular_strategy_position_exit_management/py.typed",
        ):
            archive.writestr(name, "")
    sdist = tmp_path / "module.tar.gz"
    root = tmp_path / "source"
    for relative in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "tests/conftest.py",
        "benchmarks/benchmark.py",
        "tools/verify_release_artifacts.py",
        "tools/replay_evidence.py",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(root, arcname="module-0.2.1")
    VERIFIER.verify_wheel(wheel)
    VERIFIER.verify_sdist(sdist)


def test_release_verifier_rejects_secret_or_cache_members(tmp_path: Path):
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(".env", "secret")
    with pytest.raises(ValueError, match="forbidden"):
        VERIFIER.verify_wheel(wheel)
