from __future__ import annotations

import ast
from pathlib import Path


def test_production_source_contains_no_optimization_sensitive_asserts() -> None:
    source_root = Path(__file__).parents[1] / "src" / "modular_research_runner"
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                violations.append(f"{path.relative_to(source_root)}:{node.lineno}")
    assert violations == []
