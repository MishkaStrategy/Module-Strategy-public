from __future__ import annotations

import ast
from pathlib import Path


def test_production_package_does_not_depend_on_assert_statements() -> None:
    package_root = Path(__file__).parents[1] / "src" / "market_regime"
    offenders: list[str] = []
    for path in sorted(package_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
