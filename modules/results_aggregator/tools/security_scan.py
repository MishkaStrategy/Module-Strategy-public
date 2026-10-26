#!/usr/bin/env python3
"""Deterministic local static security and secret scanner.

This scanner is intentionally conservative and dependency-free. It never imports
or executes the scanned package.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import re
from typing import Iterable

BANNED_IMPORT_ROOTS = {
    "ftplib",
    "httpx",
    "marshal",
    "paramiko",
    "pickle",
    "requests",
    "shelve",
    "socket",
    "subprocess",
    "urllib",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
SECRET_PATTERNS = (
    ("AWS_ACCESS_KEY", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GITHUB_TOKEN", re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "ASSIGNED_SECRET",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*"
            r"['\"][^'\"\n]{12,}['\"]"
        ),
    ),
)
TEXT_SUFFIXES = {".py", ".toml", ".md", ".json", ".yaml", ".yml", ".ini", ".cfg"}
EXCLUDED_PARTS = {".git", ".pytest_cache", "__pycache__", "build", "dist", "htmlcov"}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    path: str
    line: int
    detail: str
    severity: str = "HIGH"


def _iter_files(roots: Iterable[Path]) -> tuple[Path, ...]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDED_PARTS or part.startswith(".venv") for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES:
                files.append(path)
    return tuple(sorted(set(files), key=lambda item: item.as_posix()))


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _scan_python(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as error:
        return [Finding("PYTHON_SYNTAX", path.as_posix(), error.lineno or 0, str(error))]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in BANNED_IMPORT_ROOTS:
                    findings.append(
                        Finding("BANNED_IMPORT", path.as_posix(), node.lineno, alias.name)
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in BANNED_IMPORT_ROOTS:
                findings.append(Finding("BANNED_IMPORT", path.as_posix(), node.lineno, node.module))
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func) or ""
            if name in BANNED_CALLS:
                findings.append(Finding("DYNAMIC_EXECUTION", path.as_posix(), node.lineno, name))
            if name in {"os.system", "os.popen"}:
                findings.append(Finding("SHELL_EXECUTION", path.as_posix(), node.lineno, name))
            if name.endswith((".execute", ".executemany", ".executescript")) and node.args:
                query = node.args[0]
                if isinstance(query, ast.JoinedStr):
                    findings.append(
                        Finding("DYNAMIC_SQL_FSTRING", path.as_posix(), node.lineno, name)
                    )
                elif isinstance(query, ast.Call) and isinstance(query.func, ast.Attribute):
                    if query.func.attr in {"format", "join"}:
                        findings.append(
                            Finding("DYNAMIC_SQL_FORMAT", path.as_posix(), node.lineno, name)
                        )
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True:
                        findings.append(Finding("SHELL_TRUE", path.as_posix(), node.lineno, name))
    return findings


def scan(roots: Iterable[Path]) -> dict[str, object]:
    files = _iter_files(roots)
    findings: list[Finding] = []
    file_hashes: dict[str, str] = {}
    for path in files:
        raw = path.read_bytes()
        file_hashes[path.as_posix()] = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("INVALID_UTF8", path.as_posix(), 0, "text file is not UTF-8"))
            continue
        if path.suffix == ".py":
            findings.extend(_scan_python(path, text))
        for rule, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(Finding(rule, path.as_posix(), line, "potential secret material"))

    ordered = sorted(findings, key=lambda item: (item.path, item.line, item.rule, item.detail))
    payload: dict[str, object] = {
        "schema_version": "security-scan.v1",
        "status": "PASS" if not ordered else "FAIL",
        "files_scanned": len(files),
        "findings": [asdict(item) for item in ordered],
        "file_hashes": dict(sorted(file_hashes.items())),
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    payload["report_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = scan(args.roots)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
