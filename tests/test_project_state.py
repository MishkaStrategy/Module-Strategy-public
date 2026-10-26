from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_state", ROOT / "scripts/project_state.py")
assert SPEC is not None and SPEC.loader is not None
project_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project_state)


def canonical() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / "PROJECT_STATE.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_canonical_state_and_generation_are_deterministic() -> None:
    state = project_state.validate_state(canonical())
    assert project_state.render_summary(state) == project_state.render_summary(copy.deepcopy(state))


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda s: s.update(extra=True), "unknown fields"),
        (lambda s: s["workstreams"][0].update(status="mystery"), "status is unknown"),
        (lambda s: s["workstreams"][0].update(canonical_branch="main"), "main/ref authority"),
        (lambda s: s["source_checkpoint"].update(integration_head="deadbeef"), "40-character"),
        (lambda s: s["workstreams"][1].update(merge_policy="review-required"), "PR #325"),
        (lambda s: s["authority"].update(live_execution="allowed"), "unsafe or unknown"),
        (
            lambda s: s["handoff"].update(parallel_same_branch_mutation="allowed"),
            "handoff",
        ),
        (
            lambda s: s["branch_cleanup"].update(delete_candidates=["legacy/example"]),
            "delete candidates",
        ),
    ],
)
def test_fail_closed_negative_contract(mutate: Any, match: str) -> None:
    state = canonical()
    mutate(state)
    with pytest.raises(project_state.StateError, match=match):
        project_state.validate_state(state)


def test_duplicate_id_is_rejected() -> None:
    state = canonical()
    state["workstreams"][1]["id"] = state["workstreams"][0]["id"]
    with pytest.raises(project_state.StateError, match="duplicate workstream id"):
        project_state.validate_state(state)


def test_duplicate_owner_is_rejected() -> None:
    state = canonical()
    state["workstreams"][1]["owner_key"] = state["workstreams"][0]["owner_key"]
    with pytest.raises(project_state.StateError, match="duplicate workstream ownership"):
        project_state.validate_state(state)


def test_validation_only_surface_cannot_authorize_merge() -> None:
    state = canonical()
    state["workstreams"][1]["status"] = "active"
    with pytest.raises(project_state.StateError, match="contradictory|PR #325"):
        project_state.validate_state(state)


def test_open_surface_counts_match_complete_classification() -> None:
    state = project_state.validate_state(canonical())
    surface = state["open_surface_classification"]
    for key, count_key in (
        ("pull_requests", "observed_open_pull_requests"),
        ("issues", "observed_open_issues"),
    ):
        identities = {identity for values in surface[key].values() for identity in values}
        assert len(identities) == surface[count_key]


def test_open_surface_rejects_duplicate_classification() -> None:
    state = canonical()
    duplicate = state["open_surface_classification"]["pull_requests"]["active"][0]
    state["open_surface_classification"]["pull_requests"]["frozen-history"].append(duplicate)
    state["open_surface_classification"]["pull_requests"]["frozen-history"].sort()
    with pytest.raises(project_state.StateError, match="more than once"):
        project_state.validate_state(state)


def test_open_surface_rejects_missing_identity() -> None:
    state = canonical()
    state["open_surface_classification"]["issues"]["active"].pop()
    with pytest.raises(project_state.StateError, match="observed count"):
        project_state.validate_state(state)


def test_pr_325_must_remain_validation_only_in_open_surface() -> None:
    state = canonical()
    validation = state["open_surface_classification"]["pull_requests"]["validation-only"]
    validation.remove(325)
    active = state["open_surface_classification"]["pull_requests"]["active"]
    active.append(325)
    active.sort()
    with pytest.raises(project_state.StateError, match="PR #325"):
        project_state.validate_state(state)


def test_branch_cleanup_is_retain_only_with_verified_ancestry() -> None:
    state = project_state.validate_state(canonical())
    cleanup = state["branch_cleanup"]
    assert cleanup["proposal"] == "retain-history-no-deletion"
    assert cleanup["delete_candidates"] == []
    assert len(cleanup["verified_ancestry"]) >= 2
    assert {entry["relation"] for entry in cleanup["verified_ancestry"]} == {
        "exact-ancestor-of-canonical-integration"
    }
    assert {entry["disposition"] for entry in cleanup["verified_ancestry"]} == {
        "retain-frozen-history"
    }


def test_parallel_work_handoff_is_fail_closed() -> None:
    state = project_state.validate_state(canonical())
    assert state["handoff"] == {
        "live_github_precedence": "required",
        "one_owner_per_workstream": "required",
        "parallel_same_branch_mutation": "forbidden",
        "stale_snapshot_action": "reverify-and-refresh",
        "merge_authority_from_snapshot": "forbidden",
    }
