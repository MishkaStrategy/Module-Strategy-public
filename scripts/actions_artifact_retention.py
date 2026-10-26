#!/usr/bin/env python3
"""Safely inventory or delete old GitHub Actions diagnostic artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_ALLOWED_CONCLUSIONS = frozenset({"failure", "cancelled"})
_API_VERSION = "2022-11-28"
_PER_PAGE = 100
_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 6
_DEFAULT_MAX_DELETE_BYTES = 512 * 1024 * 1024


class ArtifactRetentionError(RuntimeError):
    """Raised when GitHub artifact retention cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    artifact_id: int
    name: str
    run_id: int
    conclusion: str
    created_at: datetime
    size_in_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "run_id": self.run_id,
            "conclusion": self.conclusion,
            "created_at": self.created_at.isoformat(),
            "size_in_bytes": self.size_in_bytes,
        }


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted: tuple[ArtifactCandidate, ...]
    already_missing: tuple[ArtifactCandidate, ...]


def select_candidate(
    artifact: dict[str, Any], run: dict[str, Any], *, cutoff: datetime
) -> ArtifactCandidate | None:
    if artifact.get("expired") is True:
        return None
    artifact_id = artifact.get("id")
    name = artifact.get("name")
    size_in_bytes = artifact.get("size_in_bytes")
    created_at_raw = artifact.get("created_at")
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(artifact_id, int)
        or isinstance(artifact_id, bool)
        or artifact_id <= 0
        or not isinstance(name, str)
        or not name
        or not isinstance(size_in_bytes, int)
        or isinstance(size_in_bytes, bool)
        or size_in_bytes < 0
        or not isinstance(created_at_raw, str)
        or not isinstance(workflow_run, dict)
    ):
        raise ArtifactRetentionError("artifact metadata is malformed")
    run_id = workflow_run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise ArtifactRetentionError("artifact workflow_run.id is malformed")
    created_at = _parse_github_time(created_at_raw)
    if created_at >= cutoff:
        return None
    if run.get("id") != run_id:
        raise ArtifactRetentionError("workflow run identity does not match artifact")
    if run.get("status") != "completed":
        return None
    conclusion = run.get("conclusion")
    if conclusion not in _ALLOWED_CONCLUSIONS:
        return None
    return ArtifactCandidate(
        artifact_id=artifact_id,
        name=name,
        run_id=run_id,
        conclusion=str(conclusion),
        created_at=created_at,
        size_in_bytes=size_in_bytes,
    )


def inventory_candidates(
    *, repository: str, token: str, older_than_days: int
) -> list[ArtifactCandidate]:
    if older_than_days <= 0:
        raise ArtifactRetentionError("older_than_days must be positive")
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    run_cache: dict[int, dict[str, Any]] = {}
    candidates: list[ArtifactCandidate] = []
    for artifact in _list_artifacts(repository, token):
        if not _artifact_needs_run_lookup(artifact, cutoff=cutoff):
            continue
        workflow_run = artifact.get("workflow_run")
        if not isinstance(workflow_run, dict):
            raise ArtifactRetentionError("artifact workflow_run metadata is malformed")
        run_id = workflow_run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise ArtifactRetentionError("artifact workflow_run.id is malformed")
        run = run_cache.get(run_id)
        if run is None:
            run = _github_json(repository, token, f"/repos/{repository}/actions/runs/{run_id}")
            if not isinstance(run, dict):
                raise ArtifactRetentionError("workflow run response is malformed")
            run_cache[run_id] = run
        candidate = select_candidate(artifact, run, cutoff=cutoff)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.created_at, item.artifact_id))
    return candidates


def _artifact_needs_run_lookup(artifact: dict[str, Any], *, cutoff: datetime) -> bool:
    if artifact.get("expired") is True:
        return False
    created_at_raw = artifact.get("created_at")
    if not isinstance(created_at_raw, str):
        raise ArtifactRetentionError("artifact created_at metadata is malformed")
    return _parse_github_time(created_at_raw) < cutoff


def delete_candidates(
    candidates: list[ArtifactCandidate],
    *,
    repository: str,
    token: str,
    max_delete: int,
    max_delete_bytes: int,
) -> DeletionResult:
    if max_delete <= 0:
        raise ArtifactRetentionError("max_delete must be positive")
    if max_delete_bytes <= 0:
        raise ArtifactRetentionError("max_delete_bytes must be positive")
    if len(candidates) > max_delete:
        raise ArtifactRetentionError(
            f"eligible artifact count {len(candidates)} exceeds max_delete={max_delete}"
        )
    total_bytes = sum(item.size_in_bytes for item in candidates)
    if total_bytes > max_delete_bytes:
        raise ArtifactRetentionError(
            f"eligible artifact bytes {total_bytes} exceeds max_delete_bytes={max_delete_bytes}"
        )
    deleted: list[ArtifactCandidate] = []
    already_missing: list[ArtifactCandidate] = []
    for candidate in candidates:
        removed = _github_delete(
            repository,
            token,
            f"/repos/{repository}/actions/artifacts/{candidate.artifact_id}",
        )
        if removed:
            deleted.append(candidate)
        else:
            already_missing.append(candidate)
    return DeletionResult(tuple(deleted), tuple(already_missing))


def summarize(candidates: list[ArtifactCandidate]) -> dict[str, object]:
    total_bytes = sum(item.size_in_bytes for item in candidates)
    return {
        "eligible_count": len(candidates),
        "eligible_bytes": total_bytes,
        "eligible_mib": round(total_bytes / (1024 * 1024), 3),
        "by_conclusion": {
            conclusion: sum(1 for item in candidates if item.conclusion == conclusion)
            for conclusion in sorted(_ALLOWED_CONCLUSIONS)
        },
        "largest": [
            item.to_dict()
            for item in sorted(
                candidates,
                key=lambda item: (item.size_in_bytes, item.artifact_id),
                reverse=True,
            )[:20]
        ],
    }


def _list_artifacts(repository: str, token: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": _PER_PAGE, "page": page})
        payload = _github_json(repository, token, f"/repos/{repository}/actions/artifacts?{query}")
        if not isinstance(payload, dict):
            raise ArtifactRetentionError("artifacts response is malformed")
        rows = payload.get("artifacts")
        if not isinstance(rows, list):
            raise ArtifactRetentionError("artifacts response is missing artifact list")
        for row in rows:
            if not isinstance(row, dict):
                raise ArtifactRetentionError("artifact row is malformed")
            artifacts.append(row)
        if len(rows) < _PER_PAGE:
            break
        page += 1
    return artifacts


def _github_json(repository: str, token: str, path: str) -> object:
    response = _github_request(repository, token, path, method="GET")
    if response is None:
        raise ArtifactRetentionError("GitHub GET unexpectedly returned not found")
    try:
        return json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactRetentionError("GitHub API returned invalid JSON") from exc


def _github_delete(repository: str, token: str, path: str) -> bool:
    response = _github_request(repository, token, path, method="DELETE", allow_not_found=True)
    if response is None:
        return False
    if response:
        raise ArtifactRetentionError("GitHub delete response must be empty")
    return True


def _github_request(
    repository: str,
    token: str,
    path: str,
    *,
    method: str,
    allow_not_found: bool = False,
) -> bytes | None:
    if not repository or "/" not in repository:
        raise ArtifactRetentionError("repository must use owner/name form")
    if not token:
        raise ArtifactRetentionError("GitHub token is required")
    url = f"https://api.github.com{path}"
    last_error: BaseException | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": "module-strategy-artifact-retention",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_not_found:
                return None
            body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in _RETRYABLE_HTTP_STATUS and attempt < _MAX_ATTEMPTS:
                last_error = exc
                _sleep_before_retry(
                    attempt, exc.headers.get("Retry-After") if exc.headers else None
                )
                continue
            raise ArtifactRetentionError(
                f"GitHub API {method} failed with HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < _MAX_ATTEMPTS:
                last_error = exc
                _sleep_before_retry(attempt, None)
                continue
            raise ArtifactRetentionError(f"GitHub API {method} failed") from exc
    raise ArtifactRetentionError(f"GitHub API {method} failed after retries") from last_error


def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
    delay = 0.0
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = 0.0
    if delay <= 0:
        delay = min(float(2 ** (attempt - 1)), 16.0)
    time.sleep(min(delay, 30.0))


def _parse_github_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactRetentionError("GitHub timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactRetentionError("GitHub timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--older-than-days", type=int, default=14)
    parser.add_argument("--max-delete", type=int, default=1000)
    parser.add_argument("--max-delete-bytes", type=int, default=_DEFAULT_MAX_DELETE_BYTES)
    parser.add_argument("--delete", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repository = args.repository
    if not isinstance(repository, str) or not repository:
        raise SystemExit("--repository or GITHUB_REPOSITORY is required")
    token = os.environ.get(args.token_env, "")
    try:
        candidates = inventory_candidates(
            repository=repository,
            token=token,
            older_than_days=args.older_than_days,
        )
        summary = summarize(candidates)
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        if args.delete:
            result = delete_candidates(
                candidates,
                repository=repository,
                token=token,
                max_delete=args.max_delete,
                max_delete_bytes=args.max_delete_bytes,
            )
            deleted_bytes = sum(item.size_in_bytes for item in result.deleted)
            missing_bytes = sum(item.size_in_bytes for item in result.already_missing)
            print(
                json.dumps(
                    {
                        "deleted_count": len(result.deleted),
                        "deleted_bytes": deleted_bytes,
                        "deleted_mib": round(deleted_bytes / (1024 * 1024), 3),
                        "already_missing_count": len(result.already_missing),
                        "already_missing_bytes": missing_bytes,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    except ArtifactRetentionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
