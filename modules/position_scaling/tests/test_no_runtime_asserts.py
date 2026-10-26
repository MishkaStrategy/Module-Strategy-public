from __future__ import annotations

import ast
from pathlib import Path


def test_production_source_contains_no_runtime_asserts() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "modular_strategy_position_scaling"
    for path in source.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree)), path
