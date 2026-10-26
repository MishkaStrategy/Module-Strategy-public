from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "local_pretest.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("local_pretest_parent_symlink", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_result_reset_rejects_symlinked_local_tests_parent(tmp_path: Path) -> None:
    module = _load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_parent = tmp_path / "outside" / ".local-tests"
    outside_parent.mkdir(parents=True)
    marker = outside_parent / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    local_tests = repo / ".local-tests"
    local_tests.symlink_to(outside_parent, target_is_directory=True)
    workspace = local_tests / "pretest"

    with pytest.raises(module.PretestError, match="parent must not be a symlink"):
        module._reset_generated_results(workspace, ("smoke",))

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (outside_parent / "pretest").exists()
