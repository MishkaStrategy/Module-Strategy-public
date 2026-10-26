from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from modular_results_registry.errors import IntegrityError
from modular_results_registry.storage import _sql_identifier


def _scanner_module():
    path = Path(__file__).parents[1] / "tools" / "security_scan.py"
    spec = importlib.util.spec_from_file_location("local_security_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sql_identifier_allowlist_rejects_unknown_identifier() -> None:
    assert _sql_identifier("experiments") == "experiments"
    with pytest.raises(IntegrityError, match="unsafe SQL identifier"):
        _sql_identifier("experiments; DROP TABLE experiments")


def test_security_scanner_detects_dynamic_execution_network_and_secret(tmp_path: Path) -> None:
    module = _scanner_module()
    source = tmp_path / "unsafe.py"
    source.write_text(
        "import socket\ntoken = '" + "gh" + "p_abcdefghijklmnopqrstuvwxyz123456'\neval('1 + 1')\n",
        encoding="utf-8",
    )
    report = module.scan((tmp_path,))
    assert report["status"] == "FAIL"
    rules = {item["rule"] for item in report["findings"]}
    assert {"BANNED_IMPORT", "DYNAMIC_EXECUTION", "GITHUB_TOKEN"} <= rules


def test_security_scanner_is_deterministic_for_safe_tree(tmp_path: Path) -> None:
    module = _scanner_module()
    source = tmp_path / "safe.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = module.scan((tmp_path,))
    second = module.scan((tmp_path,))
    assert first == second
    assert first["status"] == "PASS"
