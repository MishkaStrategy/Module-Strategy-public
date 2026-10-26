#!/usr/bin/env python3
"""Read-only inventory of unexpired successful GitHub Actions artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.actions_artifact_retention import (  # noqa: E402
    ArtifactRetentionError,
    _github_json,
    _list_artifacts,
    _parse_github_time,
)

_DEFAULT_MAX_RUN_LOOKUPS = 2500
_DEFAULT_DISPOSITION = "PROTECTED_UNTIL_OWNER_REVIEW"


@dataclass(frozen=True, slots=True)
class SuccessfulArtifactRecord:
    artifact_id: int
    artifact_name: str
    size_in_bytes: int
    created_at: datetime
    expires_at: datetime
    run_id: int
    workflow_name: str
    workflow_path: str
    event: str
    head_sha: str
    head_branch: str | None
    disposition: str = _DEFAULT_DISPOSITION

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_name": self.artifact_name,
            "size_in_bytes": self.size_in_bytes,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "workflow_path": self.workflow_path,
            "event": self.event,
            "head_sha": self.head_sha,
            "head_branch": self.head_branch,
            "disposition": self.disposition,
        }


def _required_positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ArtifactRetentionError(f"{field} is malformed")
    return value


def _required_nonnegative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ArtifactRetentionError(f"{field} is malformed")
    return value


def _required_public_label(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ArtifactRetentionError(f"{field} is malformed")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ArtifactRetentionError(f"{field} contains unsafe control characters")
    if value.startswith(("/", "~")) or (
        len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"}
    ):
        raise ArtifactRetentionError(f"{field} must not contain an absolute host path")
    return value


def _optional_public_label(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_public_label(value, field=field)


def _parse_github_datetime(value: str, *, field: str) -> datetime:
    parsed = _parse_github_time(value)
    if not isinstance(parsed, datetime):
        raise ArtifactRetentionError(f"{field} is malformed")
    return parsed


def _artifact_run_id(artifact: dict[str, Any]) -> int:
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise ArtifactRetentionError("artifact workflow_run metadata is malformed")
    return _required_positive_int(workflow_run.get("id"), field="artifact workflow_run.id")


def _artifact_is_currently_unexpired(artifact: dict[str, Any], *, now: datetime) -> bool:
    if artifact.get("expired") is True:
        return False
    expires_at_raw = artifact.get("expires_at")
    if not isinstance(expires_at_raw, str):
        raise ArtifactRetentionError("artifact expires_at metadata is malformed")
    expires_at = _parse_github_datetime(expires_at_raw, field="artifact expires_at")
    return expires_at > now


def select_successful_artifact(
    artifact: dict[str, Any],
    run: dict[str, Any],
    *,
    now: datetime,
) -> SuccessfulArtifactRecord | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ArtifactRetentionError("inventory timestamp must be timezone-aware")
    now = now.astimezone(UTC)

    if not _artifact_is_currently_unexpired(artifact, now=now):
        return None

    artifact_id = _required_positive_int(artifact.get("id"), field="artifact id")
    artifact_name = _required_public_label(artifact.get("name"), field="artifact name")
    size_in_bytes = _required_nonnegative_int(
        artifact.get("size_in_bytes"), field="artifact size_in_bytes"
    )
    created_at_raw = artifact.get("created_at")
    expires_at_raw = artifact.get("expires_at")
    if not isinstance(created_at_raw, str) or not isinstance(expires_at_raw, str):
        raise ArtifactRetentionError("artifact timestamps are malformed")
    created_at = _parse_github_datetime(created_at_raw, field="artifact created_at")
    expires_at = _parse_github_datetime(expires_at_raw, field="artifact expires_at")
    if created_at > expires_at:
        raise ArtifactRetentionError("artifact expiry precedes creation")

    run_id = _artifact_run_id(artifact)
    if run.get("id") != run_id:
        raise ArtifactRetentionError("workflow run identity does not match artifact")
    if run.get("status") != "completed":
        return None
    if run.get("conclusion") != "success":
        return None

    workflow_name = _required_public_label(run.get("name"), field="workflow name")
    workflow_path = _required_public_label(run.get("path"), field="workflow path")
    if not workflow_path.startswith(".github/workflows/"):
        raise ArtifactRetentionError("workflow path must stay under .github/workflows")
    event = _required_public_label(run.get("event"), field="workflow event")
    head_sha = _required_public_label(run.get("head_sha"), field="workflow head_sha")
    head_branch = _optional_public_label(run.get("head_branch"), field="workflow head_branch")

    return SuccessfulArtifactRecord(
        artifact_id=artifact_id,
        artifact_name=artifact_name,
        size_in_bytes=size_in_bytes,
        created_at=created_at,
        expires_at=expires_at,
        run_id=run_id,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        event=event,
        head_sha=head_sha,
        head_branch=head_branch,
    )


def inventory_successful_artifacts(
    *,
    repository: str,
    token: str,
    now: datetime | None = None,
    max_run_lookups: int = _DEFAULT_MAX_RUN_LOOKUPS,
) -> list[SuccessfulArtifactRecord]:
    if max_run_lookups <= 0:
        raise ArtifactRetentionError("max_run_lookups must be positive")
    observed_at = datetime.now(UTC) if now is None else now
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ArtifactRetentionError("inventory timestamp must be timezone-aware")
    observed_at = observed_at.astimezone(UTC)

    artifacts = _list_artifacts(repository, token)
    artifact_ids: set[int] = set()
    required_run_ids: set[int] = set()
    current_artifacts: list[dict[str, Any]] = []

    for artifact in artifacts:
        artifact_id = _required_positive_int(artifact.get("id"), field="artifact id")
        if artifact_id in artifact_ids:
            raise ArtifactRetentionError("artifact pagination returned duplicate identities")
        artifact_ids.add(artifact_id)
        if not _artifact_is_currently_unexpired(artifact, now=observed_at):
            continue
        current_artifacts.append(artifact)
        required_run_ids.add(_artifact_run_id(artifact))

    if len(required_run_ids) > max_run_lookups:
        raise ArtifactRetentionError(
            "unexpired artifact run lookup count exceeds max_run_lookups: "
            f"{len(required_run_ids)} > {max_run_lookups}"
        )

    runs: dict[int, dict[str, Any]] = {}
    for run_id in sorted(required_run_ids):
        payload = _github_json(
            repository,
            token,
            f"/repos/{repository}/actions/runs/{run_id}",
        )
        if not isinstance(payload, dict):
            raise ArtifactRetentionError("workflow run response is malformed")
        if payload.get("id") != run_id:
            raise ArtifactRetentionError("workflow run identity does not match requested run")
        runs[run_id] = payload

    records: list[SuccessfulArtifactRecord] = []
    for artifact in current_artifacts:
        run_id = _artifact_run_id(artifact)
        record = select_successful_artifact(
            artifact,
            runs[run_id],
            now=observed_at,
        )
        if record is not None:
            records.append(record)

    records.sort(key=lambda item: (item.created_at, item.artifact_id))
    return records


def inventory_digest(records: list[SuccessfulArtifactRecord]) -> str:
    ordered = sorted(records, key=lambda item: (item.created_at, item.artifact_id))
    payload = {
        "schema_version": 1,
        "records": [record.to_dict() for record in ordered],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def summarize(records: list[SuccessfulArtifactRecord]) -> dict[str, object]:
    ordered = sorted(records, key=lambda item: (item.created_at, item.artifact_id))
    grouped: dict[tuple[str, str, str], list[SuccessfulArtifactRecord]] = {}
    for record in ordered:
        key = (record.workflow_name, record.workflow_path, record.artifact_name)
        grouped.setdefault(key, []).append(record)

    groups: list[dict[str, object]] = []
    for (workflow_name, workflow_path, artifact_name), members in sorted(grouped.items()):
        groups.append(
            {
                "workflow_name": workflow_name,
                "workflow_path": workflow_path,
                "artifact_name": artifact_name,
                "count": len(members),
                "bytes": sum(item.size_in_bytes for item in members),
                "artifact_ids": [item.artifact_id for item in members],
                "first_created_at": min(item.created_at for item in members).isoformat(),
                "last_created_at": max(item.created_at for item in members).isoformat(),
                "earliest_expires_at": min(item.expires_at for item in members).isoformat(),
                "latest_expires_at": max(item.expires_at for item in members).isoformat(),
                "disposition": _DEFAULT_DISPOSITION,
            }
        )

    total_bytes = sum(item.size_in_bytes for item in ordered)
    return {
        "schema_version": 1,
        "default_disposition": _DEFAULT_DISPOSITION,
        "successful_unexpired_count": len(ordered),
        "successful_unexpired_bytes": total_bytes,
        "successful_unexpired_mib": round(total_bytes / (1024 * 1024), 3),
        "inventory_sha256": inventory_digest(ordered),
        "records": [record.to_dict() for record in ordered],
        "groups": groups,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--max-run-lookups", type=int, default=_DEFAULT_MAX_RUN_LOOKUPS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repository = args.repository
    if not isinstance(repository, str) or not repository:
        raise SystemExit("--repository or GITHUB_REPOSITORY is required")
    token = os.environ.get(args.token_env, "")
    try:
        records = inventory_successful_artifacts(
            repository=repository,
            token=token,
            max_run_lookups=args.max_run_lookups,
        )
        print(
            json.dumps(
                summarize(records),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except ArtifactRetentionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
