from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "module-strategy-project-state/v1"
CANONICAL_INTEGRATION = "integration/modular-strategy-full-rc"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]+(?<![/.])$")
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,63}$")
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{1,95}$")

STATUSES = {
    "active",
    "blocked",
    "deferred",
    "external-approval",
    "validation-only",
    "frozen-history",
    "superseded",
    "completed",
}
MERGE_POLICIES = {
    "review-required",
    "blocked-no-merge",
    "deferred-no-merge",
    "external-approval-required",
    "never-merge-validation-only",
    "frozen-history-no-rewrite",
    "superseded-no-merge",
    "already-merged",
    "no-pr",
}
CLASSIFICATIONS = (
    "active",
    "validation-only",
    "frozen-history",
    "superseded",
    "external-approval",
)
TOP_KEYS = {
    "schema",
    "canonical_integration_branch",
    "source_checkpoint",
    "authority",
    "handoff",
    "open_surface_classification",
    "branch_cleanup",
    "workstreams",
}
CHECKPOINT_KEYS = {"repository", "integration_head", "main_head", "governance_issue"}
AUTHORITY_KEYS = {"merge_target", "main_writes", "release", "live_execution", "mode"}
HANDOFF_KEYS = {
    "live_github_precedence",
    "one_owner_per_workstream",
    "parallel_same_branch_mutation",
    "stale_snapshot_action",
    "merge_authority_from_snapshot",
}
OPEN_SURFACE_KEYS = {
    "observed_open_pull_requests",
    "observed_open_issues",
    "pull_requests",
    "issues",
}
BRANCH_CLEANUP_KEYS = {"proposal", "verified_ancestry", "delete_candidates"}
ANCESTRY_KEYS = {"branch", "sha", "relation", "evidence_pr", "disposition"}
WORKSTREAM_KEYS = {
    "id",
    "status",
    "owner_key",
    "owner_area",
    "canonical_branch",
    "issue",
    "pr",
    "dependencies",
    "supersedes",
    "merge_policy",
    "current_sha",
    "accepted_sha",
}


class StateError(ValueError):
    pass


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise StateError(f"{where} must be a string-keyed mapping")
    return value


def _exact_keys(value: dict[str, Any], allowed: set[str], required: set[str], where: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise StateError(f"{where} has unknown fields: {sorted(unknown)}")
    if missing:
        raise StateError(f"{where} is missing fields: {sorted(missing)}")


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise StateError(f"{where} must be a 40-character lowercase git SHA")
    return value


def _branch(value: Any, where: str) -> str:
    if not isinstance(value, str) or not BRANCH_RE.fullmatch(value):
        raise StateError(f"{where} is not a valid branch identity")
    if value == "main" or value.startswith("refs/"):
        raise StateError(f"{where} may not claim main/ref authority")
    return value


def _positive_int_or_none(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StateError(f"{where} must be a positive integer or null")
    return value


def _classification_surface(
    raw: Any,
    *,
    where: str,
    expected_count: int,
    require_pr_325: bool,
) -> dict[str, list[int]]:
    surface = _mapping(raw, where)
    _exact_keys(surface, set(CLASSIFICATIONS), set(CLASSIFICATIONS), where)
    seen: set[int] = set()
    parsed: dict[str, list[int]] = {}
    for classification in CLASSIFICATIONS:
        values = surface[classification]
        item_where = f"{where}.{classification}"
        if not isinstance(values, list):
            raise StateError(f"{item_where} must be a list")
        if values != sorted(values):
            raise StateError(f"{item_where} must be numerically sorted")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values
        ):
            raise StateError(f"{item_where} must contain positive integer identities")
        if len(values) != len(set(values)):
            raise StateError(f"{item_where} contains duplicate identities")
        overlap = seen.intersection(values)
        if overlap:
            raise StateError(f"{where} classifies identities more than once: {sorted(overlap)}")
        seen.update(values)
        parsed[classification] = values
    if len(seen) != expected_count:
        raise StateError(
            f"{where} classifies {len(seen)} identities but observed count is {expected_count}"
        )
    if require_pr_325 and 325 not in parsed["validation-only"]:
        raise StateError("PR #325 must be classified validation-only")
    return parsed


def load_state(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StateError(f"cannot load {path}: {exc}") from exc
    return validate_state(parsed)


def validate_state(raw: Any) -> dict[str, Any]:
    state = _mapping(raw, "state")
    _exact_keys(state, TOP_KEYS, TOP_KEYS, "state")
    if state["schema"] != SCHEMA:
        raise StateError(f"unsupported schema: {state['schema']!r}")
    if state["canonical_integration_branch"] != CANONICAL_INTEGRATION:
        raise StateError("canonical integration branch is not the approved full-RC branch")

    checkpoint = _mapping(state["source_checkpoint"], "source_checkpoint")
    _exact_keys(checkpoint, CHECKPOINT_KEYS, CHECKPOINT_KEYS, "source_checkpoint")
    if checkpoint["repository"] != "MishkaStrategy/Module-Strategy":
        raise StateError("source_checkpoint.repository is not canonical")
    _sha(checkpoint["integration_head"], "source_checkpoint.integration_head")
    _sha(checkpoint["main_head"], "source_checkpoint.main_head")
    if checkpoint["integration_head"] == checkpoint["main_head"]:
        raise StateError("integration and main checkpoints must be independently observed")
    if checkpoint["governance_issue"] != 389:
        raise StateError("source checkpoint must be governed by Issue #389")

    authority = _mapping(state["authority"], "authority")
    _exact_keys(authority, AUTHORITY_KEYS, AUTHORITY_KEYS, "authority")
    expected_authority = {
        "merge_target": CANONICAL_INTEGRATION,
        "main_writes": "forbidden",
        "release": "forbidden",
        "live_execution": "forbidden",
        "mode": "paper-research-only",
    }
    if authority != expected_authority:
        raise StateError("unsafe or unknown project authority declaration")

    handoff = _mapping(state["handoff"], "handoff")
    _exact_keys(handoff, HANDOFF_KEYS, HANDOFF_KEYS, "handoff")
    expected_handoff = {
        "live_github_precedence": "required",
        "one_owner_per_workstream": "required",
        "parallel_same_branch_mutation": "forbidden",
        "stale_snapshot_action": "reverify-and-refresh",
        "merge_authority_from_snapshot": "forbidden",
    }
    if handoff != expected_handoff:
        raise StateError("unsafe or unknown parallel-work handoff declaration")

    open_surface = _mapping(state["open_surface_classification"], "open_surface_classification")
    _exact_keys(open_surface, OPEN_SURFACE_KEYS, OPEN_SURFACE_KEYS, "open_surface_classification")
    pr_count = _positive_int_or_none(
        open_surface["observed_open_pull_requests"],
        "open_surface_classification.observed_open_pull_requests",
    )
    issue_count = _positive_int_or_none(
        open_surface["observed_open_issues"],
        "open_surface_classification.observed_open_issues",
    )
    assert pr_count is not None
    assert issue_count is not None
    pr_surface = _classification_surface(
        open_surface["pull_requests"],
        where="open_surface_classification.pull_requests",
        expected_count=pr_count,
        require_pr_325=True,
    )
    issue_surface = _classification_surface(
        open_surface["issues"],
        where="open_surface_classification.issues",
        expected_count=issue_count,
        require_pr_325=False,
    )

    cleanup = _mapping(state["branch_cleanup"], "branch_cleanup")
    _exact_keys(cleanup, BRANCH_CLEANUP_KEYS, BRANCH_CLEANUP_KEYS, "branch_cleanup")
    if cleanup["proposal"] != "retain-history-no-deletion":
        raise StateError("branch cleanup proposal may not authorize destructive cleanup")
    if cleanup["delete_candidates"] != []:
        raise StateError("branch cleanup v1 must not contain delete candidates")
    ancestry = cleanup["verified_ancestry"]
    if not isinstance(ancestry, list) or not ancestry:
        raise StateError("branch_cleanup.verified_ancestry must be a non-empty list")
    ancestry_branches: set[str] = set()
    ancestry_shas: set[str] = set()
    for index, raw_entry in enumerate(ancestry):
        where = f"branch_cleanup.verified_ancestry[{index}]"
        entry = _mapping(raw_entry, where)
        _exact_keys(entry, ANCESTRY_KEYS, ANCESTRY_KEYS, where)
        branch = _branch(entry["branch"], f"{where}.branch")
        sha = _sha(entry["sha"], f"{where}.sha")
        if branch in ancestry_branches or sha in ancestry_shas:
            raise StateError("branch cleanup ancestry contains duplicate branch/SHA identity")
        ancestry_branches.add(branch)
        ancestry_shas.add(sha)
        if entry["relation"] != "exact-ancestor-of-canonical-integration":
            raise StateError(f"{where}.relation is not an accepted ancestry proof")
        _positive_int_or_none(entry["evidence_pr"], f"{where}.evidence_pr")
        if entry["disposition"] != "retain-frozen-history":
            raise StateError(f"{where}.disposition may not authorize deletion")

    workstreams = state["workstreams"]
    if not isinstance(workstreams, list) or not workstreams:
        raise StateError("workstreams must be a non-empty list")

    ids: set[str] = set()
    owners: set[str] = set()
    issues: set[int] = set()
    prs: set[int] = set()
    parsed_workstreams: list[dict[str, Any]] = []
    for index, item in enumerate(workstreams):
        where = f"workstreams[{index}]"
        ws = _mapping(item, where)
        required = WORKSTREAM_KEYS - {"current_sha", "accepted_sha"}
        _exact_keys(ws, WORKSTREAM_KEYS, required, where)

        ws_id = ws["id"]
        if not isinstance(ws_id, str) or not ID_RE.fullmatch(ws_id):
            raise StateError(f"{where}.id is malformed")
        if ws_id in ids:
            raise StateError(f"duplicate workstream id: {ws_id}")
        ids.add(ws_id)

        owner_key = ws["owner_key"]
        if not isinstance(owner_key, str) or not OWNER_RE.fullmatch(owner_key):
            raise StateError(f"{where}.owner_key is malformed")
        if owner_key in owners:
            raise StateError(f"duplicate workstream ownership: {owner_key}")
        owners.add(owner_key)

        owner_area = ws["owner_area"]
        if not isinstance(owner_area, str) or not OWNER_RE.fullmatch(owner_area):
            raise StateError(f"{where}.owner_area is malformed")
        _branch(ws["canonical_branch"], f"{where}.canonical_branch")

        issue = _positive_int_or_none(ws["issue"], f"{where}.issue")
        pr = _positive_int_or_none(ws["pr"], f"{where}.pr")
        if issue is not None:
            if issue in issues:
                raise StateError(f"duplicate issue ownership: #{issue}")
            issues.add(issue)
        if pr is not None:
            if pr in prs:
                raise StateError(f"duplicate PR ownership: #{pr}")
            prs.add(pr)

        status = ws["status"]
        policy = ws["merge_policy"]
        if status not in STATUSES:
            raise StateError(f"{where}.status is unknown: {status!r}")
        if policy not in MERGE_POLICIES:
            raise StateError(f"{where}.merge_policy is unknown: {policy!r}")

        if pr == 325:
            if status != "validation-only" or policy != "never-merge-validation-only":
                raise StateError("PR #325 is validation-only and can never be merge-authorized")
            if ws["canonical_branch"] != CANONICAL_INTEGRATION:
                raise StateError("PR #325 must identify the canonical full-RC validation branch")

        allowed_by_status = {
            "active": {"review-required", "no-pr"},
            "blocked": {"blocked-no-merge"},
            "deferred": {"deferred-no-merge"},
            "external-approval": {"external-approval-required"},
            "validation-only": {"never-merge-validation-only"},
            "frozen-history": {"frozen-history-no-rewrite"},
            "superseded": {"superseded-no-merge"},
            "completed": {"already-merged", "no-pr"},
        }
        if policy not in allowed_by_status[status]:
            raise StateError(f"{where} has contradictory status/merge_policy")
        if (
            policy
            in {
                "review-required",
                "already-merged",
                "never-merge-validation-only",
                "frozen-history-no-rewrite",
                "superseded-no-merge",
            }
            and pr is None
        ):
            raise StateError(f"{where} merge policy requires a PR identity")
        if policy == "no-pr" and pr is not None:
            raise StateError(f"{where} no-pr policy contradicts PR identity")

        for key in ("dependencies", "supersedes"):
            values = ws[key]
            if not isinstance(values, list) or not all(
                isinstance(value, str) and ID_RE.fullmatch(value) for value in values
            ):
                raise StateError(f"{where}.{key} must be a list of workstream ids")
            if len(values) != len(set(values)) or ws_id in values:
                raise StateError(f"{where}.{key} contains duplicate/self references")

        if "current_sha" in ws:
            _sha(ws["current_sha"], f"{where}.current_sha")
        if "accepted_sha" in ws:
            _sha(ws["accepted_sha"], f"{where}.accepted_sha")
        parsed_workstreams.append(ws)

    known = {ws["id"] for ws in parsed_workstreams}
    for ws in parsed_workstreams:
        missing_refs = (set(ws["dependencies"]) | set(ws["supersedes"])) - known
        if missing_refs:
            raise StateError(f"{ws['id']} references unknown workstreams: {sorted(missing_refs)}")

    open_prs = {identity for values in pr_surface.values() for identity in values}
    open_issues = {identity for values in issue_surface.values() for identity in values}
    for ws in parsed_workstreams:
        pr = ws["pr"]
        issue = ws["issue"]
        if ws["status"] in {
            "active",
            "blocked",
            "deferred",
            "external-approval",
            "validation-only",
        }:
            if pr is not None and pr not in open_prs:
                raise StateError(
                    f"{ws['id']} claims an open-status PR absent from open classification"
                )
            if issue is not None and issue not in open_issues:
                raise StateError(
                    f"{ws['id']} claims an open-status Issue absent from open classification"
                )
    return state


def _surface_values(surface: dict[str, Any], key: str) -> str:
    values = surface[key]
    assert isinstance(values, list)
    return ", ".join(f"#{value}" for value in values) or "—"


def render_summary(state: dict[str, Any]) -> str:
    checkpoint = state["source_checkpoint"]
    open_surface = state["open_surface_classification"]
    cleanup = state["branch_cleanup"]
    lines = [
        "<!-- GENERATED by scripts/project_state.py; DO NOT EDIT. -->",
        "# Canonical Project State — generated summary",
        "",
        f"Schema: `{state['schema']}`",
        "",
        "This file is a deterministic projection of `PROJECT_STATE.yaml`. It is not a second truth store.",
        "The checkpoint SHAs identify the GitHub state observed when the YAML snapshot was refreshed;",
        "they do not claim to be the commit containing this generated file.",
        "",
        "## Snapshot",
        "",
        f"- canonical integration branch: `{state['canonical_integration_branch']}`",
        f"- observed integration head: `{checkpoint['integration_head']}`",
        f"- observed main head: `{checkpoint['main_head']}`",
        f"- governing issue: `#{checkpoint['governance_issue']}`",
        f"- authority: `{state['authority']['mode']}`; main/release/LIVE are forbidden",
        "",
        "## Open GitHub surface classification",
        "",
        f"- observed open pull requests: `{open_surface['observed_open_pull_requests']}`",
        f"- observed open issues: `{open_surface['observed_open_issues']}`",
        "",
        "| Surface | Active | Validation-only | Frozen history | Superseded | External approval |",
        "|---|---|---|---|---|---|",
    ]
    for label, surface_name in (("Pull requests", "pull_requests"), ("Issues", "issues")):
        surface = open_surface[surface_name]
        lines.append(
            f"| {label} | {_surface_values(surface, 'active')} | "
            f"{_surface_values(surface, 'validation-only')} | "
            f"{_surface_values(surface, 'frozen-history')} | "
            f"{_surface_values(surface, 'superseded')} | "
            f"{_surface_values(surface, 'external-approval')} |"
        )
    lines.extend(
        [
            "",
            "## Branch cleanup proposal",
            "",
            f"- proposal: `{cleanup['proposal']}`",
            "- delete candidates: none",
        ]
    )
    for entry in cleanup["verified_ancestry"]:
        lines.append(
            f"- retain `{entry['branch']}` at `{entry['sha']}`: "
            f"`{entry['relation']}` (PR #{entry['evidence_pr']})"
        )
    lines.extend(
        [
            "",
            "## Parallel-work handoff rules",
            "",
            "- live GitHub must be re-read before work starts or resumes;",
            "- one owner key governs each workstream; parallel mutation of one branch is forbidden;",
            "- stale snapshots require live re-verification and refresh;",
            "- this snapshot grants no merge, main, release, deployment, or LIVE authority;",
            "",
            "## Workstreams",
            "",
            "| ID | Status | Owner area | Canonical branch | Issue | PR | Merge policy | Dependencies | Current/accepted SHA |",
            "|---|---|---|---|---:|---:|---|---|---|",
        ]
    )
    for ws in state["workstreams"]:
        issue = f"#{ws['issue']}" if ws["issue"] is not None else "—"
        pr = f"#{ws['pr']}" if ws["pr"] is not None else "—"
        deps = ", ".join(ws["dependencies"]) or "—"
        sha = ws.get("current_sha") or ws.get("accepted_sha") or "—"
        if "current_sha" in ws and "accepted_sha" in ws:
            sha = f"current `{ws['current_sha'][:12]}` / accepted `{ws['accepted_sha'][:12]}`"
        elif sha != "—":
            label = "current" if "current_sha" in ws else "accepted"
            sha = f"{label} `{sha[:12]}`"
        lines.append(
            f"| `{ws['id']}` | `{ws['status']}` | `{ws['owner_area']}` | "
            f"`{ws['canonical_branch']}` | {issue} | {pr} | `{ws['merge_policy']}` | {deps} | {sha} |"
        )
    lines.extend(
        [
            "",
            "## Semantics",
            "",
            "Refresh `PROJECT_STATE.yaml` only after live GitHub verification, then regenerate this file.",
            "The complete open PR/Issue lists are a bounded classification snapshot; any live count or identity drift",
            "makes them stale and requires a fresh classification pass rather than an inferred update.",
            "Historical issue/PR text may contain stale SHAs and must not override the source checkpoint.",
            "The validator fails closed on unknown fields/enums, malformed identities, duplicate ownership,",
            "contradictory merge policy, incomplete/overlapping open-surface classification, PR #325 merge",
            "authorization, destructive branch cleanup, unsafe handoff policy, and unsafe main/release/LIVE authority.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "generate", "check-generated"))
    parser.add_argument("--state", type=Path, default=Path("PROJECT_STATE.yaml"))
    parser.add_argument("--summary", type=Path, default=Path("docs/hq/PROJECT_STATE.generated.md"))
    args = parser.parse_args()
    try:
        state = load_state(args.state)
        rendered = render_summary(state)
        if args.command == "generate":
            args.summary.write_text(rendered, encoding="utf-8")
        elif args.command == "check-generated":
            actual = args.summary.read_text(encoding="utf-8")
            if actual != rendered:
                raise StateError(f"{args.summary} is stale; run project_state.py generate")
    except (OSError, StateError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
