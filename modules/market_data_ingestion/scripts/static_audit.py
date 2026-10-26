#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

_DANGEROUS_CALLS = frozenset({"eval", "exec"})
_UNSAFE_IMPORTS = frozenset({"dill", "marshal", "pickle", "shelve"})
_SUBPROCESS_CALLS = frozenset({"Popen", "call", "check_call", "check_output", "run"})


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    path: str
    line: int
    detail: str


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _source_files(root: Path) -> Iterable[Path]:
    return sorted(path for path in root.rglob("*.py") if path.is_file())


def audit(root: Path, *, line_length: int) -> dict[str, object]:
    findings: list[Finding] = []
    files = list(_source_files(root))
    for path in files:
        relative = path.relative_to(root.parent).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            findings.append(
                Finding(
                    "SYNTAX_ERROR",
                    relative,
                    exc.lineno or 0,
                    exc.msg,
                )
            )
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if len(line) > line_length:
                findings.append(
                    Finding(
                        "LINE_TOO_LONG",
                        relative,
                        line_number,
                        f"{len(line)} characters",
                    )
                )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.returns is None:
                    findings.append(
                        Finding(
                            "RETURN_ANNOTATION_MISSING",
                            relative,
                            node.lineno,
                            node.name,
                        )
                    )
                defaults = list(node.args.defaults)
                defaults.extend(item for item in node.args.kw_defaults if item is not None)
                if any(isinstance(item, (ast.Dict, ast.List, ast.Set)) for item in defaults):
                    findings.append(
                        Finding(
                            "MUTABLE_DEFAULT",
                            relative,
                            node.lineno,
                            node.name,
                        )
                    )
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    findings.append(
                        Finding(
                            "EMPTY_FUNCTION",
                            relative,
                            node.lineno,
                            node.name,
                        )
                    )
            elif isinstance(node, ast.Assert):
                findings.append(
                    Finding(
                        "RUNTIME_ASSERT",
                        relative,
                        node.lineno,
                        "assert removed under python -O",
                    )
                )
            elif isinstance(node, ast.ExceptHandler) and node.type is None:
                findings.append(
                    Finding(
                        "BARE_EXCEPT",
                        relative,
                        node.lineno,
                        "bare except",
                    )
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    top_level = name.split(".", 1)[0]
                    if top_level in _UNSAFE_IMPORTS:
                        findings.append(
                            Finding(
                                "UNSAFE_SERIALIZATION_IMPORT",
                                relative,
                                node.lineno,
                                top_level,
                            )
                        )
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in _DANGEROUS_CALLS:
                    findings.append(
                        Finding(
                            "DYNAMIC_CODE_EXECUTION",
                            relative,
                            node.lineno,
                            name,
                        )
                    )
                if name in _SUBPROCESS_CALLS:
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "shell"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                        ):
                            findings.append(
                                Finding(
                                    "SUBPROCESS_SHELL_TRUE",
                                    relative,
                                    node.lineno,
                                    name,
                                )
                            )
    codes: dict[str, int] = {}
    for finding in findings:
        codes[finding.code] = codes.get(finding.code, 0) + 1
    return {
        "status": "ok" if not findings else "failed",
        "root": str(root.resolve()),
        "files_checked": len(files),
        "line_length": line_length,
        "finding_count": len(findings),
        "finding_counts": dict(sorted(codes.items())),
        "findings": [asdict(item) for item in findings],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run dependency-free AST and source safety checks."
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("src/market_data_ingestion"),
    )
    parser.add_argument("--line-length", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.line_length <= 0:
        parser.error("--line-length must be positive")
    report = audit(args.root, line_length=args.line_length)
    encoded = json.dumps(
        report,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
