#!/usr/bin/env python3
"""Audit and inventory GitHub Actions artifact upload policy."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOTS = (Path(".github/workflows"), Path(".github/actions"))
RESIDUAL_DEBT_REGISTRY = Path(".github/policies/artifact-upload-residual-debt.json")
UPLOAD_RE = re.compile(r"^\s*-?\s*uses:\s*actions/upload-artifact@")
STEP_RE = re.compile(r"^(?P<indent>\s*)-\s+")
RETENTION_RE = re.compile(r"^\s*retention-days\s*:\s*(?P<value>.*?)\s*$")
BLOCK_SCALARS = {">", ">-", ">+", "|", "|-", "|+"}
REGISTRY_ENTRY_KEYS = {
    "path",
    "step_name",
    "artifact_name",
    "condition",
    "classification",
    "owner",
    "disposition",
    "reference",
}


@dataclass(frozen=True)
class UploadStep:
    path: Path
    line: int
    name: str | None
    artifact_name: str | None
    condition: str | None
    has_failure: bool
    has_cancelled: bool
    continue_on_error: bool
    classification: str


@dataclass(frozen=True)
class UploadIdentity:
    path: str
    step_name: str | None
    artifact_name: str | None
    condition: str | None
    classification: str


def yaml_files() -> list[Path]:
    files: list[Path] = []
    for root in ROOTS:
        if not root.exists():
            continue
        files.extend(root.rglob("*.yml"))
        files.extend(root.rglob("*.yaml"))
    return sorted(set(files))


def step_bounds(lines: list[str], uses_index: int) -> tuple[int, int]:
    uses_indent = len(lines[uses_index]) - len(lines[uses_index].lstrip())
    step_start = uses_index
    step_indent = uses_indent

    if not lines[uses_index].lstrip().startswith("- "):
        for index in range(uses_index - 1, -1, -1):
            match = STEP_RE.match(lines[index])
            if match is None:
                continue
            candidate_indent = len(match.group("indent"))
            if candidate_indent < uses_indent:
                step_start = index
                step_indent = candidate_indent
                break

    step_end = len(lines)
    for index in range(step_start + 1, len(lines)):
        match = STEP_RE.match(lines[index])
        if match is None:
            continue
        if len(match.group("indent")) == step_indent:
            step_end = index
            break
    return step_start, step_end


def _step_indent(lines: list[str], step_start: int) -> int:
    match = STEP_RE.match(lines[step_start])
    if match is None:
        raise ValueError(f"line {step_start + 1} is not a YAML step")
    return len(match.group("indent"))


def _step_key_value(
    lines: list[str],
    step_start: int,
    step_end: int,
    key: str,
) -> str | None:
    step_indent = _step_indent(lines, step_start)
    key_indent = step_indent + 2
    prefix = f"{key}:"

    for index in range(step_start, step_end):
        line = lines[index]
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if index == step_start and indent == step_indent:
            inline_prefix = f"- {prefix}"
            if not stripped.startswith(inline_prefix):
                continue
            value = stripped[len(inline_prefix) :].strip()
        elif indent == key_indent and stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
        else:
            continue

        if value not in BLOCK_SCALARS:
            return value or None

        parts: list[str] = []
        for continuation in range(index + 1, step_end):
            candidate = lines[continuation]
            if not candidate.strip():
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= key_indent:
                break
            parts.append(candidate.strip())
        return " ".join(parts) or None
    return None


def _nested_step_key_value(
    lines: list[str],
    step_start: int,
    step_end: int,
    parent: str,
    key: str,
) -> str | None:
    step_indent = _step_indent(lines, step_start)
    parent_indent = step_indent + 2
    child_indent = parent_indent + 2
    parent_prefix = f"{parent}:"
    key_prefix = f"{key}:"

    for index in range(step_start, step_end):
        line = lines[index]
        indent = len(line) - len(line.lstrip())
        if indent != parent_indent or line.strip() != parent_prefix:
            continue
        for child_index in range(index + 1, step_end):
            child = lines[child_index]
            if not child.strip():
                continue
            current_indent = len(child) - len(child.lstrip())
            if current_indent <= parent_indent:
                break
            stripped = child.strip()
            if current_indent == child_indent and stripped.startswith(key_prefix):
                value = stripped[len(key_prefix) :].strip()
                return value or None
        return None
    return None


def _classification(condition: str | None, continue_on_error: bool) -> str:
    if condition is None:
        base = "unconditional"
    else:
        has_failure = "failure()" in condition
        has_cancelled = "cancelled()" in condition
        has_success = "success()" in condition
        if has_failure and has_cancelled:
            base = "failure-cancelled-condition"
        elif has_failure:
            base = "failure-condition"
        elif has_cancelled:
            base = "cancelled-condition"
        elif has_success:
            base = "success-condition"
        else:
            base = "other-condition"
    suffix = "nonblocking" if continue_on_error else "blocking"
    return f"{base}-{suffix}"


def _retention_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    return value


def classify_file(path: Path) -> tuple[list[UploadStep], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    uploads: list[UploadStep] = []
    errors: list[str] = []

    for index, line in enumerate(lines):
        if not UPLOAD_RE.match(line):
            continue
        step_start, step_end = step_bounds(lines, index)
        name = _step_key_value(lines, step_start, step_end, "name")
        artifact_name = _nested_step_key_value(lines, step_start, step_end, "with", "name")
        condition = _step_key_value(lines, step_start, step_end, "if")
        continue_value = _step_key_value(lines, step_start, step_end, "continue-on-error")
        continue_on_error = continue_value == "true"
        has_failure = condition is not None and "failure()" in condition
        has_cancelled = condition is not None and "cancelled()" in condition
        uploads.append(
            UploadStep(
                path=path,
                line=index + 1,
                name=name,
                artifact_name=artifact_name,
                condition=condition,
                has_failure=has_failure,
                has_cancelled=has_cancelled,
                continue_on_error=continue_on_error,
                classification=_classification(condition, continue_on_error),
            )
        )
        for candidate_index in range(step_start, step_end):
            match = RETENTION_RE.match(lines[candidate_index])
            if match is None:
                continue
            if _retention_value(match.group("value")) == "1":
                continue
            errors.append(
                f"{path}:{candidate_index + 1}: explicit retention-days must be exactly 1 "
                "under the organization CI storage policy"
            )
    return uploads, errors


def audit_file(path: Path) -> tuple[int, list[str]]:
    uploads, errors = classify_file(path)
    return len(uploads), errors


def requires_residual_debt(upload: UploadStep) -> bool:
    return not upload.continue_on_error and not upload.has_failure and not upload.has_cancelled


def upload_identity(upload: UploadStep) -> UploadIdentity:
    return UploadIdentity(
        path=upload.path.as_posix(),
        step_name=upload.name,
        artifact_name=upload.artifact_name if upload.name is None else None,
        condition=upload.condition,
        classification=upload.classification,
    )


def _identity_sort_key(identity: UploadIdentity) -> tuple[str, str, str, str, str]:
    return (
        identity.path,
        identity.step_name or "",
        identity.artifact_name or "",
        identity.condition or "",
        identity.classification,
    )


def _format_identity(identity: UploadIdentity) -> str:
    return json.dumps(
        {
            "path": identity.path,
            "step_name": identity.step_name,
            "artifact_name": identity.artifact_name,
            "condition": identity.condition,
            "classification": identity.classification,
        },
        sort_keys=True,
    )


def _optional_string(
    entry: dict[str, object],
    field: str,
    location: str,
    errors: list[str],
) -> str | None:
    value = entry[field]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location}: {field} must be null or a non-empty string")
        return None
    return value


def _required_string(
    entry: dict[str, object],
    field: str,
    location: str,
    errors: list[str],
) -> str | None:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location}: {field} must be a non-empty string")
        return None
    return value


def load_residual_debt_registry(
    path: Path = RESIDUAL_DEBT_REGISTRY,
) -> tuple[set[UploadIdentity], list[str]]:
    errors: list[str] = []
    try:
        raw_payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return set(), [f"{path}: cannot load residual artifact debt registry: {exc}"]

    if not isinstance(raw_payload, dict):
        return set(), [f"{path}: registry root must be an object"]
    payload = cast(dict[str, object], raw_payload)
    if payload.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be exactly 1")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return set(), [*errors, f"{path}: entries must be a list"]

    identities: set[UploadIdentity] = set()
    for index, raw_entry in enumerate(cast(list[object], raw_entries)):
        location = f"{path}:entries[{index}]"
        if not isinstance(raw_entry, dict):
            errors.append(f"{location}: entry must be an object")
            continue
        entry = cast(dict[str, object], raw_entry)
        if set(entry) != REGISTRY_ENTRY_KEYS:
            errors.append(
                f"{location}: keys must be exactly {sorted(REGISTRY_ENTRY_KEYS)!r}; "
                f"got {sorted(entry)!r}"
            )
            continue

        path_value = _required_string(entry, "path", location, errors)
        classification = _required_string(entry, "classification", location, errors)
        owner = _required_string(entry, "owner", location, errors)
        disposition = _required_string(entry, "disposition", location, errors)
        reference = _required_string(entry, "reference", location, errors)
        step_name = _optional_string(entry, "step_name", location, errors)
        artifact_name = _optional_string(entry, "artifact_name", location, errors)
        condition = _optional_string(entry, "condition", location, errors)
        if None in (path_value, classification, owner, disposition, reference):
            continue
        assert path_value is not None
        assert classification is not None

        if step_name is None and artifact_name is None:
            errors.append(
                f"{location}: either step_name or artifact_name is required for stable identity"
            )
            continue
        expected_classification = _classification(condition, False)
        if classification != expected_classification:
            errors.append(
                f"{location}: classification {classification!r} does not match "
                f"condition-derived {expected_classification!r}"
            )
            continue
        if condition is not None and ("failure()" in condition or "cancelled()" in condition):
            errors.append(
                f"{location}: failure/cancelled diagnostics cannot be registered as "
                "residual successful blocking debt"
            )
            continue

        identity = UploadIdentity(
            path=path_value,
            step_name=step_name,
            artifact_name=artifact_name if step_name is None else None,
            condition=condition,
            classification=classification,
        )
        if identity in identities:
            errors.append(f"{location}: duplicate identity {_format_identity(identity)}")
            continue
        identities.add(identity)
    return identities, errors


def validate_residual_debt(
    uploads: list[UploadStep],
    registry_path: Path = RESIDUAL_DEBT_REGISTRY,
) -> list[str]:
    registered, errors = load_residual_debt_registry(registry_path)
    if errors:
        return errors

    identities: list[UploadIdentity] = []
    for upload in uploads:
        if not requires_residual_debt(upload):
            continue
        if upload.name is None and upload.artifact_name is None:
            errors.append(
                f"{upload.path}:{upload.line}: blocking upload lacks both step name and "
                "with.name; a stable identity is required"
            )
            continue
        identities.append(upload_identity(upload))

    counts = Counter(identities)
    for identity, count in sorted(counts.items(), key=lambda item: _identity_sort_key(item[0])):
        if count > 1:
            errors.append(
                "blocking upload identity is not unique; add or refine a stable step/artifact "
                f"name: count={count} identity={_format_identity(identity)}"
            )

    actual = set(identities)
    for identity in sorted(actual - registered, key=_identity_sort_key):
        errors.append(f"unregistered blocking artifact upload: {_format_identity(identity)}")
    for identity in sorted(registered - actual, key=_identity_sort_key):
        errors.append(
            f"registered residual artifact debt is missing or changed: {_format_identity(identity)}"
        )
    return errors


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _print_inventory(uploads: list[UploadStep]) -> None:
    counts = Counter(upload.classification for upload in uploads)
    for upload in uploads:
        name = "null" if upload.name is None else json.dumps(upload.name)
        artifact_name = "null" if upload.artifact_name is None else json.dumps(upload.artifact_name)
        condition = "null" if upload.condition is None else json.dumps(upload.condition)
        print(
            "UPLOAD_CLASSIFICATION "
            f"{upload.path}:{upload.line} "
            f"name={name} "
            f"artifact_name={artifact_name} "
            f"class={upload.classification} "
            f"failure={_bool(upload.has_failure)} "
            f"cancelled={_bool(upload.has_cancelled)} "
            f"continue_on_error={_bool(upload.continue_on_error)} "
            f"if={condition}"
        )
    summary = " ".join(f"{name}={counts[name]}" for name in sorted(counts))
    print(f"UPLOAD_CLASSIFICATION_SUMMARY total={len(uploads)} {summary}".rstrip())


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    allowed = {"--report-classification"}
    unknown = [argument for argument in arguments if argument not in allowed]
    if unknown:
        print(f"unsupported argument(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    uploads: list[UploadStep] = []
    errors: list[str] = []
    for path in yaml_files():
        file_uploads, file_errors = classify_file(path)
        uploads.extend(file_uploads)
        errors.extend(file_errors)
    errors.extend(validate_residual_debt(uploads))

    if "--report-classification" in arguments:
        _print_inventory(uploads)

    if errors:
        print("Artifact retention policy failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    residual_count = sum(requires_residual_debt(upload) for upload in uploads)
    explicit_one_day = 0
    for path in yaml_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            match = RETENTION_RE.match(line)
            if match is not None and _retention_value(match.group("value")) == "1":
                explicit_one_day += 1
    print(
        "Artifact retention policy passed: "
        f"{len(uploads)} upload-artifact step(s); "
        f"{explicit_one_day} explicit one-day diagnostic retention declaration(s); "
        f"{residual_count} blocking success/conditional upload identity/identities "
        "exactly match the explicit residual-debt registry."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
