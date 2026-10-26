from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class EvidencePrivacyError(RuntimeError):
    """Raised when CI evidence is unsafe to publish."""


@dataclass(frozen=True)
class _TextFile:
    path: Path
    text: str


_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("service API token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"),
    ),
    (
        "bearer credential",
        re.compile(
            r"(?i)\b(?:authorization\s*:\s*)?bearer\s+"
            r"(?!\*{3,}|<redacted>|redacted)[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|token|access[_-]?token|api[_-]?key|private[_-]?key)"
            r"\s*[:=]\s*(?!\*{3,}|<redacted>|redacted|none\b|null\b)[^\s,;]{8,}"
        ),
    ),
    (
        "credential-bearing URL",
        re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
    ),
)

_RUNNER_ROOT_SEGMENT = "GitHub" + "Actions" + "Runners"
_REPOSITORY_WORKSPACE_SEGMENT = "_" + "work" + "/" + "Module" + "-" + "Strategy"
_PRIVATE_TOPOLOGY_REPLACEMENTS: tuple[tuple[str, str, str], ...] = (
    ("runner root topology", _RUNNER_ROOT_SEGMENT, "redacted-runner-root"),
    (
        "repository workspace topology",
        _REPOSITORY_WORKSPACE_SEGMENT,
        "redacted-workspace",
    ),
)


def _iter_regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise EvidencePrivacyError("evidence directory is missing")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EvidencePrivacyError("evidence contains a symbolic link")
        if path.is_file():
            files.append(path)
    if not files:
        raise EvidencePrivacyError("evidence directory is empty")
    return files


def _read_text_files(paths: Iterable[Path]) -> list[_TextFile]:
    files: list[_TextFile] = []
    for path in paths:
        payload = path.read_bytes()
        if b"\x00" in payload:
            raise EvidencePrivacyError("evidence contains a binary file")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidencePrivacyError("evidence contains non-UTF-8 data") from exc
        files.append(_TextFile(path=path, text=text))
    return files


def _credential_findings(files: Iterable[_TextFile]) -> set[str]:
    findings: set[str] = set()
    for item in files:
        for label, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(item.text):
                findings.add(label)
    return findings


def _private_roots(root: Path) -> tuple[str, ...]:
    candidates = {
        value
        for key in (
            "HOME",
            "RUNNER_TEMP",
            "GITHUB_WORKSPACE",
            "RUNNER_TOOL_CACHE",
            "UV_CACHE_DIR",
            "VIRTUAL_ENV",
            "TMPDIR",
            "TMP",
            "TEMP",
        )
        if (value := os.environ.get(key))
    }
    candidates.add(str(Path.cwd().resolve()))
    candidates.add(str(root.resolve()))
    return tuple(sorted(candidates, key=len, reverse=True))


def _runner_identities() -> tuple[str, ...]:
    identities = {
        value
        for value in (
            os.environ.get("RUNNER_NAME"),
            os.environ.get("HOSTNAME"),
            os.environ.get("COMPUTERNAME"),
            socket.gethostname(),
            platform.node(),
        )
        if value and len(value) >= 4
    }
    return tuple(sorted(identities, key=len, reverse=True))


def _replace_literal_variants(
    text: str,
    marker: str,
    replacement: str,
) -> tuple[str, int]:
    replacements = 0
    for variant in {marker, marker.replace("/", "\\")}:
        if not variant:
            continue
        count = text.count(variant)
        if count:
            text = text.replace(variant, replacement)
            replacements += count
    return text, replacements


def _scrub_text(
    text: str,
    *,
    private_roots: tuple[str, ...],
    runner_user: str,
    runner_identities: tuple[str, ...],
) -> tuple[str, int]:
    replacements = 0

    def substitute(pattern: re.Pattern[str], replacement: str, value: str) -> str:
        nonlocal replacements
        value, count = pattern.subn(replacement, value)
        replacements += count
        return value

    text = substitute(
        re.compile(r"file://[^\s\"'<>]+"),
        "redacted-local-uri",
        text,
    )

    # Replace the most specific dynamic roots before generic user-home prefixes.
    # Otherwise a prefix replacement can leave the internal runner topology behind.
    for private_root in private_roots:
        text, count = _replace_literal_variants(text, private_root, "redacted-path")
        replacements += count

    text = substitute(
        re.compile(r"/Users/[^/\s\"'<>]+"),
        "redacted-home",
        text,
    )
    text = substitute(
        re.compile(r"/home/[^/\s\"'<>]+"),
        "redacted-home",
        text,
    )
    text = substitute(
        re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+"),
        "redacted-home",
        text,
    )

    if len(runner_user) >= 4:
        count = text.count(runner_user)
        if count:
            text = text.replace(runner_user, "redacted-user")
            replacements += count

    for runner_identity in runner_identities:
        count = text.count(runner_identity)
        if count:
            text = text.replace(runner_identity, "redacted-runner")
            replacements += count

    for _, marker, replacement in _PRIVATE_TOPOLOGY_REPLACEMENTS:
        text, count = _replace_literal_variants(text, marker, replacement)
        replacements += count

    return text, replacements


def _remaining_privacy_findings(
    files: Iterable[_TextFile],
    *,
    private_roots: tuple[str, ...],
    runner_user: str,
    runner_identities: tuple[str, ...],
) -> set[str]:
    findings: set[str] = set()
    generic_markers: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("POSIX user home", re.compile(r"/(?:Users|home)/[^/\s\"'<>]+")),
        (
            "Windows user home",
            re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+"),
        ),
        ("local file URI", re.compile(r"file://")),
    )
    for item in files:
        for label, pattern in generic_markers:
            if pattern.search(item.text):
                findings.add(label)
        for private_root in private_roots:
            for variant in {private_root, private_root.replace("/", "\\")}:
                if variant and variant in item.text:
                    findings.add("private root")
        if len(runner_user) >= 4 and runner_user in item.text:
            findings.add("runner user")
        for runner_identity in runner_identities:
            if runner_identity in item.text:
                findings.add("runner identity")
        for label, marker, _ in _PRIVATE_TOPOLOGY_REPLACEMENTS:
            for variant in {marker, marker.replace("/", "\\")}:
                if variant and variant in item.text:
                    findings.add(label)
    findings.update(_credential_findings(files))
    return findings


def sanitize_evidence_directory(root: Path) -> dict[str, int | str]:
    root = root.resolve()
    paths = _iter_regular_files(root)
    source_files = _read_text_files(paths)

    credentials = _credential_findings(source_files)
    if credentials:
        raise EvidencePrivacyError(
            "credential-like evidence detected: " + ", ".join(sorted(credentials))
        )

    private_roots = _private_roots(root)
    runner_user = os.environ.get("USER", "")
    runner_identities = _runner_identities()
    replacements = 0

    sanitized_files: list[_TextFile] = []
    for item in source_files:
        text, count = _scrub_text(
            item.text,
            private_roots=private_roots,
            runner_user=runner_user,
            runner_identities=runner_identities,
        )
        replacements += count
        sanitized_files.append(_TextFile(path=item.path, text=text))

    findings = _remaining_privacy_findings(
        sanitized_files,
        private_roots=private_roots,
        runner_user=runner_user,
        runner_identities=runner_identities,
    )
    if findings:
        raise EvidencePrivacyError(
            "evidence privacy verification failed: " + ", ".join(sorted(findings))
        )

    for item in sanitized_files:
        item.path.write_text(item.text, encoding="utf-8")

    status: dict[str, int | str] = {
        "status": "PASS",
        "files_scanned": len(sanitized_files),
        "replacements": replacements,
        "credential_findings": 0,
    }
    (root / "privacy-status.json").write_text(
        json.dumps(status, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize CI evidence before publication")
    parser.add_argument("evidence_directory", type=Path)
    args = parser.parse_args()
    try:
        status = sanitize_evidence_directory(args.evidence_directory)
    except EvidencePrivacyError as exc:
        print(f"evidence privacy gate failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
