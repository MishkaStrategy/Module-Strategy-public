from __future__ import annotations

import ast
import json
from pathlib import Path


def main() -> int:
    module_dir = Path(__file__).resolve().parents[1]
    tests_dir = module_dir / "tests"
    spec_path = tests_dir / "data" / "golden_fixtures_v1.json"
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures")
    if payload.get("schema_version") != "1" or not isinstance(fixtures, list):
        raise SystemExit("invalid golden fixture schema")
    if len(fixtures) != 30:
        raise SystemExit(f"expected 30 golden fixtures, found {len(fixtures)}")
    ids = [row.get("id") for row in fixtures]
    if ids != list(range(1, 31)):
        raise SystemExit("golden fixture IDs must be exactly 1..30")
    parsed: dict[Path, set[str]] = {}
    for row in fixtures:
        node = row.get("node")
        if not isinstance(node, str) or "::" not in node:
            raise SystemExit(f"invalid test node: {node!r}")
        file_name, function_spec = node.split("::", 1)
        function_name = function_spec.split("[", 1)[0]
        path = tests_dir / file_name
        if path not in parsed:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parsed[path] = {
                item.name
                for item in tree.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        if function_name not in parsed[path]:
            raise SystemExit(f"missing golden fixture node: {node}")
        if row.get("verdict") not in {"accepted", "rejected"}:
            raise SystemExit(f"invalid verdict for fixture {row['id']}")
    print("golden_matrix_registry: PASS (30 fixtures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
