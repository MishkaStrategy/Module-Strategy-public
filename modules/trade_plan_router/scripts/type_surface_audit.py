from __future__ import annotations

import ast
from pathlib import Path
import sys


def audit_file(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        for argument in arguments:
            if argument.arg in {"self", "cls"}:
                continue
            if argument.annotation is None:
                findings.append(
                    f"{path}:{node.lineno}: public {node.name} argument "
                    f"{argument.arg} lacks annotation"
                )
        if node.returns is None:
            findings.append(f"{path}:{node.lineno}: public {node.name} lacks return annotation")
    return tuple(findings)


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "src" / "modular_strategy_trade_plan_router"
    findings = tuple(finding for path in sorted(root.glob("*.py")) for finding in audit_file(path))
    if findings:
        print("\n".join(findings))
        return 1
    print("type_surface_audit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
