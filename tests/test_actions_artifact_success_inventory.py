from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.actions_artifact_retention import ArtifactRetentionError
from scripts.actions_artifact_success_inventory import (
    _DEFAULT_DISPOSITION,
    inventory_digest,
    inventory_successful_artifacts,
    select_successful_artifact,
    summarize,
)


NOW = datetime(2026, 8, 15, 5, 0, tzinfo=UTC)


def _artifact(
    *,
    artifact_id: int = 10,
    run_id: int = 20,
    name: str = "accepted-evidence",
    created_at: str = "2026-08-10T00:00:00Z",
    expires_at: str = "2026-08-21T00:00:00Z",
    size_in_bytes: int = 1024,
    expired: bool = False,
) -> dict[str, object]:
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": size_in_bytes,
        "created_at": created_at,
        "expires_at": expires_at,
        "expired": expired,
        "workflow_run": {"id": run_id},
    }


def _run(
    *,
    run_id: int = 20,
    status: str = "completed",
    conclusion: str | None = "success",
    name: str = "Release evidence",
    path: str = ".github/workflows/release-evidence.yml",
    event: str = "push",
    head_sha: str = "a" * 40,
    head_branch: str | None = "integration/modular-strategy-full-rc",
) -> dict[str, object]:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "name": name,
        "path": path,
        "event": event,
        "head_sha": head_sha,
        "head_branch": head_branch,
    }


def _record(artifact_id: int, *, size_in_bytes: int = 1024):
    record = select_successful_artifact(
        _artifact(artifact_id=artifact_id, run_id=artifact_id + 100, size_in_bytes=size_in_bytes),
        _run(run_id=artifact_id + 100),
        now=NOW,
    )
    assert record is not None
    return record


def test_completed_success_is_inventoried_and_protected_by_default() -> None:
    record = select_successful_artifact(_artifact(), _run(), now=NOW)

    assert record is not None
    assert record.artifact_id == 10
    assert record.disposition == _DEFAULT_DISPOSITION
    assert record.workflow_path == ".github/workflows/release-evidence.yml"


@pytest.mark.parametrize(
    ("artifact", "run"),
    [
        (_artifact(expired=True), _run()),
        (_artifact(expires_at="2026-08-15T04:59:59Z"), _run()),
        (_artifact(), _run(status="in_progress", conclusion=None)),
        (_artifact(), _run(conclusion="failure")),
        (_artifact(), _run(conclusion="cancelled")),
        (_artifact(), _run(conclusion="skipped")),
    ],
)
def test_expired_active_and_non_success_artifacts_are_excluded(
    artifact: dict[str, object], run: dict[str, object]
) -> None:
    assert select_successful_artifact(artifact, run, now=NOW) is None


def test_run_identity_mismatch_fails_closed() -> None:
    with pytest.raises(ArtifactRetentionError, match="identity"):
        select_successful_artifact(_artifact(run_id=20), _run(run_id=21), now=NOW)


def test_malformed_artifact_metadata_fails_closed() -> None:
    artifact = _artifact()
    artifact["size_in_bytes"] = -1
    with pytest.raises(ArtifactRetentionError, match="malformed"):
        select_successful_artifact(artifact, _run(), now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "/Users/private/artifact"),
        ("name", "C:\\Users\\private\\artifact"),
    ],
)
def test_absolute_host_paths_in_public_labels_fail_closed(field: str, value: str) -> None:
    artifact = _artifact()
    artifact[field] = value
    with pytest.raises(ArtifactRetentionError, match="host path"):
        select_successful_artifact(artifact, _run(), now=NOW)


def test_workflow_path_must_be_repository_relative() -> None:
    with pytest.raises(ArtifactRetentionError, match="workflow path"):
        select_successful_artifact(
            _artifact(),
            _run(path="scripts/not-a-workflow.yml"),
            now=NOW,
        )


def test_digest_and_summary_are_deterministic_across_input_order() -> None:
    first = _record(1, size_in_bytes=2048)
    second = _record(2, size_in_bytes=4096)

    assert inventory_digest([first, second]) == inventory_digest([second, first])
    assert summarize([first, second]) == summarize([second, first])


def test_summary_keeps_all_successful_artifacts_protected() -> None:
    first = _record(1, size_in_bytes=2048)
    second = _record(2, size_in_bytes=4096)

    summary = summarize([first, second])

    assert summary["default_disposition"] == _DEFAULT_DISPOSITION
    assert summary["successful_unexpired_count"] == 2
    assert summary["successful_unexpired_bytes"] == 6144
    records = summary["records"]
    assert isinstance(records, list)
    assert {record["disposition"] for record in records} == {_DEFAULT_DISPOSITION}
    groups = summary["groups"]
    assert isinstance(groups, list)
    assert all(group["disposition"] == _DEFAULT_DISPOSITION for group in groups)


def test_live_inventory_uses_read_only_run_lookups_and_success_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [
        _artifact(artifact_id=1, run_id=101),
        _artifact(artifact_id=2, run_id=102),
        _artifact(
            artifact_id=3,
            run_id=103,
            expires_at=(NOW - timedelta(seconds=1)).isoformat(),
        ),
    ]
    calls: list[str] = []

    monkeypatch.setattr(
        "scripts.actions_artifact_success_inventory._list_artifacts",
        lambda repository, token: artifacts,
    )

    def fake_get(repository: str, token: str, path: str) -> object:
        calls.append(path)
        run_id = int(path.rsplit("/", 1)[1])
        conclusion = "success" if run_id == 101 else "failure"
        return _run(run_id=run_id, conclusion=conclusion)

    monkeypatch.setattr(
        "scripts.actions_artifact_success_inventory._github_json",
        fake_get,
    )

    records = inventory_successful_artifacts(
        repository="MishkaStrategy/Module-Strategy",
        token="test",
        now=NOW,
        max_run_lookups=10,
    )

    assert [record.artifact_id for record in records] == [1]
    assert calls == [
        "/repos/MishkaStrategy/Module-Strategy/actions/runs/101",
        "/repos/MishkaStrategy/Module-Strategy/actions/runs/102",
    ]


def test_live_inventory_rejects_excessive_run_lookup_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [
        _artifact(artifact_id=1, run_id=101),
        _artifact(artifact_id=2, run_id=102),
    ]
    monkeypatch.setattr(
        "scripts.actions_artifact_success_inventory._list_artifacts",
        lambda repository, token: artifacts,
    )

    with pytest.raises(ArtifactRetentionError, match="max_run_lookups"):
        inventory_successful_artifacts(
            repository="MishkaStrategy/Module-Strategy",
            token="test",
            now=NOW,
            max_run_lookups=1,
        )


def test_duplicate_artifact_identity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [
        _artifact(artifact_id=1, run_id=101),
        _artifact(artifact_id=1, run_id=102),
    ]
    monkeypatch.setattr(
        "scripts.actions_artifact_success_inventory._list_artifacts",
        lambda repository, token: artifacts,
    )

    with pytest.raises(ArtifactRetentionError, match="duplicate"):
        inventory_successful_artifacts(
            repository="MishkaStrategy/Module-Strategy",
            token="test",
            now=NOW,
        )
