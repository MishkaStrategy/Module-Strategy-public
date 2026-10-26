from __future__ import annotations

import argparse
from pathlib import Path
import sys

TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".sh"}
EXCLUDED_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}


def should_scan(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(
        part in EXCLUDED_NAMES or part.startswith(".venv") or part.endswith(".egg-info")
        for part in relative.parts
    ):
        return False
    return path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name in {"README", "CHANGELOG"})


def audit_file(path: Path) -> tuple[str, ...]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return (f"{path}: invalid UTF-8 at byte {exc.start}",)
    findings: list[str] = []
    if "\x00" in text:
        findings.append(f"{path}: contains NUL byte")
    if text and not text.endswith("\n"):
        findings.append(f"{path}: missing final newline")
    for number, line in enumerate(text.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            findings.append(f"{path}:{number}: trailing whitespace")
    return tuple(findings)


def audit_tree(root: Path) -> tuple[str, ...]:
    return tuple(
        finding
        for path in sorted(root.rglob("*"))
        if should_scan(path, root)
        for finding in audit_file(path)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    findings = audit_tree(args.root.resolve())
    if findings:
        print("\n".join(findings))
        return 1
    print("text_file_audit: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
