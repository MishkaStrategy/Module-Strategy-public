from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "httpx",
    "pickle",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "importlib.import_module",
    "pickle.loads",
    "pickle.load",
    "yaml.load",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "generic_secret_assignment": re.compile(
        r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{12,}['\"]"
    ),
}


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def scan_python(path: Path) -> list[str]:
    findings: list[str] = []
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: syntax error: {exc.msg}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_IMPORT_ROOTS:
                    findings.append(f"{path}:{node.lineno}: forbidden import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in FORBIDDEN_IMPORT_ROOTS:
                findings.append(f"{path}:{node.lineno}: forbidden import {node.module}")
        elif isinstance(node, ast.Call):
            name = dotted_name(node.func)
            if name in FORBIDDEN_CALLS:
                findings.append(f"{path}:{node.lineno}: forbidden call {name}")
            if name and name.endswith("Popen"):
                for keyword in node.keywords:
                    if (
                        keyword.arg == "shell"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        findings.append(f"{path}:{node.lineno}: subprocess shell=True")
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: potential {label}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--production-only",
        action="store_true",
        help="scan only the importable Router package; CI/recovery tooling is audited separately",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    findings: list[str] = []
    relative_roots = ("src",) if args.production_only else ("src", "benchmarks", "scripts")
    for relative in relative_roots:
        for path in sorted((root / relative).rglob("*.py")):
            if path == Path(__file__).resolve():
                continue
            findings.extend(scan_python(path))
    if findings:
        print("\n".join(findings))
        return 1
    scope = "production" if args.production_only else "full-tree"
    print(f"security_scan: PASS ({scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
