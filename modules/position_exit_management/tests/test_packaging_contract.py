from __future__ import annotations

from pathlib import Path


def test_source_distribution_manifest_keeps_reproducibility_files():
    module_root = Path(__file__).resolve().parents[1]
    manifest = (module_root / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include tests *.py" in manifest
    assert "recursive-include benchmarks *.py" in manifest
    assert "recursive-include tools *.py" in manifest
    assert "exclude tests/test_workflow_contract.py" in manifest
    assert "include config.example.yaml" in manifest
    assert (module_root / "tests" / "conftest.py").is_file()
    assert (module_root / "tools" / "verify_release_artifacts.py").is_file()
    assert (module_root / "tools" / "replay_evidence.py").is_file()
    assert (module_root / "src/modular_strategy_position_exit_management/py.typed").is_file()
