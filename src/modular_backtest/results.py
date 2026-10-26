from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

from .data import file_sha256
from .event_artifacts import EventArtifactError, shard_large_gzip_event_stream

_ARTIFACT_MANIFEST_NAME = "artifact-manifest.json"
_ARTIFACT_SCHEMA_VERSION = "1"
_HEXADECIMAL = frozenset("0123456789abcdef")


class ResultError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResultSnapshot:
    directory: Path
    run: dict[str, Any]
    metrics: dict[str, Any]
    artifact_manifest: dict[str, Any]

    @property
    def run_id(self) -> str:
        manifest = self.run["manifest"]
        return str(manifest["run_id"])

    @property
    def application_run_id(self) -> str:
        value = self.artifact_manifest.get("application_run_id")
        return str(value) if value is not None else self.run_id


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    raise ResultError(f"JSON contains non-finite numeric constant: {value}")


def _json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite_json_constant,
        )
    except ResultError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultError(f"cannot read valid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ResultError(f"{label} must contain a JSON object")
    return payload


def _artifact_files(directory: Path) -> dict[str, str]:
    if directory.is_symlink():
        raise ResultError(f"result directory must not be a symlink: {directory}")
    root_manifest = directory / _ARTIFACT_MANIFEST_NAME
    files: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path == root_manifest:
            continue
        if path.is_symlink():
            raise ResultError(f"result artifacts must not contain symlinks: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            files[relative] = file_sha256(path)
    if not files:
        raise ResultError("result directory contains no artifacts")
    return files


def write_artifact_manifest(
    directory: str | Path,
    run_id: str,
    *,
    application_run_id: str | None = None,
) -> dict[str, Any]:
    result_directory = Path(directory)
    if not result_directory.is_dir():
        raise ResultError(f"result directory does not exist: {result_directory}")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResultError("run_id must be non-empty")
    if application_run_id is not None and (
        not isinstance(application_run_id, str) or not application_run_id.strip()
    ):
        raise ResultError("application_run_id must be non-empty")
    compressed_events = result_directory / "events.jsonl.gz"
    if compressed_events.exists() or compressed_events.is_symlink():
        try:
            shard_large_gzip_event_stream(result_directory)
        except EventArtifactError as exc:
            raise ResultError("compressed event artifact normalization failed") from exc
    manifest_path = result_directory / _ARTIFACT_MANIFEST_NAME
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ResultError("artifact manifest already exists")
    payload: dict[str, Any] = {
        "schema_version": _ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "files": _artifact_files(result_directory),
    }
    if application_run_id is not None:
        payload["application_run_id"] = application_run_id
    manifest_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return payload


def verify_artifact_manifest(directory: str | Path) -> dict[str, Any]:
    result_directory = Path(directory)
    manifest_path = result_directory / _ARTIFACT_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ResultError(f"result directory must contain {_ARTIFACT_MANIFEST_NAME}")
    payload = _json_mapping(manifest_path, _ARTIFACT_MANIFEST_NAME)
    if payload.get("schema_version") != _ARTIFACT_SCHEMA_VERSION:
        raise ResultError("unsupported artifact manifest schema_version")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResultError("artifact manifest run_id must be non-empty")
    application_run_id = payload.get("application_run_id")
    if application_run_id is not None and (
        not isinstance(application_run_id, str) or not application_run_id.strip()
    ):
        raise ResultError("artifact manifest application_run_id must be non-empty")
    expected = payload.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ResultError("artifact manifest files must be a non-empty mapping")
    normalized: dict[str, str] = {}
    for name, digest in expected.items():
        unsafe_name = (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        )
        if unsafe_name:
            raise ResultError("artifact manifest contains an unsafe path")
        valid_digest = (
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in _HEXADECIMAL for character in digest)
        )
        if not valid_digest:
            raise ResultError(f"artifact manifest has an invalid SHA-256 for {name}")
        normalized[name] = digest
    actual = _artifact_files(result_directory)
    if set(actual) != set(normalized):
        missing = sorted(set(normalized) - set(actual))
        extra = sorted(set(actual) - set(normalized))
        raise ResultError(f"artifact set mismatch; missing={missing}, extra={extra}")
    mismatched = sorted(name for name in normalized if actual[name] != normalized[name])
    if mismatched:
        raise ResultError(f"artifact checksum mismatch: {mismatched}")
    verified: dict[str, Any] = {
        "schema_version": _ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "files": dict(sorted(normalized.items())),
    }
    if application_run_id is not None:
        verified["application_run_id"] = application_run_id
    return verified


def inspect_result(path: str | Path) -> ResultSnapshot:
    directory = Path(path).expanduser().resolve()
    artifact_manifest = verify_artifact_manifest(directory)
    run_path = directory / "run.json"
    metrics_path = directory / "metrics.json"
    if not run_path.is_file() or not metrics_path.is_file():
        raise ResultError("result directory must contain run.json and metrics.json")
    run = _json_mapping(run_path, "run.json")
    metrics = _json_mapping(metrics_path, "metrics.json")
    manifest = run.get("manifest")
    if not isinstance(manifest, dict):
        raise ResultError("run.json manifest must be a JSON object")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ResultError("run.json manifest.run_id must be non-empty")
    if artifact_manifest["run_id"] != run_id:
        raise ResultError("artifact manifest run_id does not match run.json")
    return ResultSnapshot(
        directory=directory,
        run=run,
        metrics=metrics,
        artifact_manifest=artifact_manifest,
    )


def _is_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def compare_results(left_path: str | Path, right_path: str | Path) -> dict[str, object]:
    left = inspect_result(left_path)
    right = inspect_result(right_path)
    keys = sorted(set(left.metrics) | set(right.metrics))
    changes: dict[str, object] = {}
    for key in keys:
        left_value = left.metrics.get(key)
        right_value = right.metrics.get(key)
        if _is_number(left_value) and _is_number(right_value):
            changes[key] = {
                "left": left_value,
                "right": right_value,
                "delta": right_value - left_value,
            }
        elif left_value != right_value:
            changes[key] = {"left": left_value, "right": right_value}
    return {
        "left_run_id": left.run_id,
        "right_run_id": right.run_id,
        "changes": changes,
    }
