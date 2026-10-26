from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def test_runtime_source_contains_no_assert_statements() -> None:
    package_root = Path(__file__).parents[1] / "src" / "modular_strategy_protection_target"
    offenders: list[str] = []
    for path in sorted(package_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_optimized_python_preserves_fail_closed_configuration() -> None:
    code = """
from modular_strategy_protection_target import (
    ProtectionTargetConfigurationError,
    ProtectionTargetModule,
)
try:
    ProtectionTargetModule(invalidation_mode="feature")
except ProtectionTargetConfigurationError:
    pass
else:
    raise SystemExit("invalid configuration was accepted under -O")
"""
    completed = subprocess.run(
        [sys.executable, "-O", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
