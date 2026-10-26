from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.actions_artifact_retention import (
    ArtifactRetentionError,
    _artifact_needs_run_lookup,
    delete_candidates,
    select_candidate,
    summarize,
)


NOW = datetime(2026, 8, 8, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=14)


def _artifact(
    *,
    artifact_id: int = 10,
    run_id: int = 20,
    created_at: str = "2026-07-01T00:00:00Z",
    size_in_bytes: int = 1024,
    expired: bool = False,
) -> dict[str, object]:
    return {
        "id": artifact_id,
        "name": f"diagnostic-{artifact_id}",
        "size_in_bytes": size_in_bytes,
        "created_at": created_at,
        "expired": expired,
        "workflow_run": {"id": run_id},
    }


def _run(*, run_id: int = 20, conclusion: str = "failure") -> dict[str, object]:
    return {"id": run_id, "status": "completed", "conclusion": conclusion}


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_old_failed_or_cancelled_artifact_is_candidate(conclusion: str) -> None:
    candidate = select_candidate(_artifact(), _run(conclusion=conclusion), cutoff=CUTOFF)

    assert candidate is not None
    assert candidate.run_id == 20
    assert candidate.conclusion == conclusion


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_successful_or_nonfailure_artifact_is_never_candidate(conclusion: str) -> None:
    assert select_candidate(_artifact(), _run(conclusion=conclusion), cutoff=CUTOFF) is None


def test_recent_expired_or_active_artifact_is_never_candidate() -> None:
    recent = _artifact(created_at="2026-08-01T00:00:00Z")
    assert select_candidate(recent, _run(), cutoff=CUTOFF) is None
    assert select_candidate(_artifact(expired=True), _run(), cutoff=CUTOFF) is None
    assert (
        select_candidate(
            _artifact(),
            {"id": 20, "status": "in_progress", "conclusion": None},
            cutoff=CUTOFF,
        )
        is None
    )


def test_run_lookup_prefilter_skips_recent_and_expired_artifacts() -> None:
    assert _artifact_needs_run_lookup(_artifact(), cutoff=CUTOFF) is True
    assert (
        _artifact_needs_run_lookup(
            _artifact(created_at="2026-08-01T00:00:00Z"),
            cutoff=CUTOFF,
        )
        is False
    )
    assert _artifact_needs_run_lookup(_artifact(expired=True), cutoff=CUTOFF) is False


def test_run_lookup_prefilter_fails_closed_on_invalid_timestamp() -> None:
    malformed = _artifact(created_at="not-a-time")
    with pytest.raises(ArtifactRetentionError, match="timestamp"):
        _artifact_needs_run_lookup(malformed, cutoff=CUTOFF)


def test_artifact_and_run_identity_must_match() -> None:
    with pytest.raises(ArtifactRetentionError, match="identity"):
        select_candidate(_artifact(run_id=20), _run(run_id=21), cutoff=CUTOFF)


def test_malformed_artifact_fails_closed() -> None:
    malformed = _artifact()
    malformed["size_in_bytes"] = -1
    with pytest.raises(ArtifactRetentionError, match="malformed"):
        select_candidate(malformed, _run(), cutoff=CUTOFF)


def test_summary_reports_bounded_nonsecret_metadata() -> None:
    first = select_candidate(
        _artifact(artifact_id=1, size_in_bytes=2 * 1024 * 1024),
        _run(),
        cutoff=CUTOFF,
    )
    second = select_candidate(
        _artifact(artifact_id=2, size_in_bytes=1024, run_id=22),
        _run(run_id=22, conclusion="cancelled"),
        cutoff=CUTOFF,
    )
    assert first is not None and second is not None

    summary = summarize([first, second])

    assert summary["eligible_count"] == 2
    assert summary["eligible_bytes"] == 2 * 1024 * 1024 + 1024
    assert summary["by_conclusion"] == {"cancelled": 1, "failure": 1}
    largest = summary["largest"]
    assert isinstance(largest, list)
    assert largest[0]["artifact_id"] == 1


def _two_candidates():
    first = select_candidate(_artifact(artifact_id=1), _run(), cutoff=CUTOFF)
    second = select_candidate(
        _artifact(artifact_id=2, run_id=22),
        _run(run_id=22),
        cutoff=CUTOFF,
    )
    assert first is not None and second is not None
    return first, second


def test_delete_is_guarded_by_max_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _two_candidates()
    called = False

    def forbidden_delete(*args: object, **kwargs: object) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("scripts.actions_artifact_retention._github_delete", forbidden_delete)
    with pytest.raises(ArtifactRetentionError, match="exceeds max_delete"):
        delete_candidates(
            [first, second],
            repository="MishkaStrategy/Module-Strategy",
            token="test",
            max_delete=1,
            max_delete_bytes=10 * 1024,
        )
    assert called is False


def test_delete_is_guarded_by_max_delete_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _two_candidates()
    called = False

    def forbidden_delete(*args: object, **kwargs: object) -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr("scripts.actions_artifact_retention._github_delete", forbidden_delete)
    with pytest.raises(ArtifactRetentionError, match="exceeds max_delete_bytes"):
        delete_candidates(
            [first, second],
            repository="MishkaStrategy/Module-Strategy",
            token="test",
            max_delete=10,
            max_delete_bytes=2047,
        )
    assert called is False


def test_delete_tracks_deleted_and_already_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _two_candidates()
    outcomes = iter([True, False])

    def fake_delete(*args: object, **kwargs: object) -> bool:
        return next(outcomes)

    monkeypatch.setattr("scripts.actions_artifact_retention._github_delete", fake_delete)
    result = delete_candidates(
        [first, second],
        repository="MishkaStrategy/Module-Strategy",
        token="test",
        max_delete=10,
        max_delete_bytes=10 * 1024,
    )

    assert result.deleted == (first,)
    assert result.already_missing == (second,)
