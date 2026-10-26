# Project State Governance

`PROJECT_STATE.yaml` at the repository root is the only machine-readable canonical project-state snapshot for HQ governance.

`docs/hq/PROJECT_STATE.generated.md` is generated deterministically from that YAML by `scripts/project_state.py`; it is a human-readable projection only and must never be edited as an independent truth source.

## Snapshot semantics

The `source_checkpoint` records the exact GitHub identities that were live-verified when the snapshot was refreshed. In particular, `integration_head` is the observed source/base checkpoint, not the SHA of the commit that contains `PROJECT_STATE.yaml`. This avoids an impossible self-referential commit-SHA contract.

A later commit, PR synchronization, integration advancement, Issue closure, or PR state change does not silently make the recorded checkpoint current. Refresh the YAML only after a new live GitHub audit, then regenerate the Markdown projection and validate both together.

Historical Issues, PR bodies, roadmap prose, and ledger prose may contain exact SHAs that were correct at their own checkpoint. Those values remain historical evidence and must not be promoted to current state unless live GitHub verification proves them current again.

## Open GitHub surface classification

`open_surface_classification` is a complete bounded classification of every open PR and every open Issue observed at the source checkpoint. It uses exactly five governance classes:

- `active` — current implementation, integration, coordination, or unresolved technical acceptance work;
- `validation-only` — open evidence/diagnostic surfaces that must not be treated as merge authority;
- `frozen-history` — intentionally retained historical/predecessor surfaces that must not receive ordinary new product work;
- `superseded` — an open historical surface whose role has been replaced by a later accepted/current owner;
- `external-approval` — the remaining action is outside ordinary repository implementation, such as explicit human approval or trusted runner/host administration.

The validator requires the identity lists to be sorted, non-overlapping, and complete for the recorded open counts. Any live count or identity change makes this section stale and requires a fresh GitHub classification pass; agents must not infer a replacement list from old prose.

PR #325 is permanently guarded as `validation-only` under this schema and can never become merge-authorized project state.

## Branch cleanup proposal

The v1 cleanup proposal is deliberately non-destructive: `retain-history-no-deletion`.

Before this proposal was recorded, GitHub ancestry was verified for the two canonical historical branch checkpoints included in `branch_cleanup.verified_ancestry`:

- frozen Core `core/integration-contract-freeze-v0.4.2`;
- frozen v0.6 predecessor `integration/modular-strategy-v0.6.0-rc`.

Each exact SHA is an ancestor of the observed canonical full-RC checkpoint. Their disposition is therefore explicit retention as frozen history, not branch deletion. `delete_candidates` must remain empty in v1.

A future destructive cleanup proposal would require a separately reviewed schema/governance change plus fresh evidence links, ancestry verification, accepted replacement ownership, and explicit authority. This issue does not grant that authority.

## Validation and authority

Run:

```bash
python scripts/project_state.py validate
python scripts/project_state.py check-generated
```

The validator is fail-closed. It rejects unknown fields/enums, malformed branch/SHA/Issue/PR identities, duplicate workstream IDs or ownership keys, duplicate Issue/PR ownership, unresolved dependency/supersession references, contradictory status/merge policy, incomplete or overlapping open-surface classification, validation-only merge authorization, any attempt to make PR #325 merge-authorized, destructive branch cleanup, unsafe parallel-work handoff rules, and unsafe `main`/release/LIVE authority.

The canonical integration target is `integration/modular-strategy-full-rc`. `main` writes, releases, deployments, and LIVE/private-exchange execution are outside this state contract and remain forbidden unless a separately reviewed governance change replaces this schema/authority contract.

## Parallel chat and worker handoff

Every project chat/worker must follow these state handoff rules:

1. Re-read live GitHub before starting or resuming work: relevant branches, Issues, PRs, reviews/threads, and exact-head CI override stale snapshot prose.
2. One `owner_key` owns one workstream. Parallel workers may operate on independent workstreams, but parallel mutation of the same branch is forbidden unless HQ explicitly reassigns ownership after a handoff.
3. If a source checkpoint, open-surface count, PR/Issue identity, or dependency changes, stop treating the snapshot as current, live-verify the change, refresh `PROJECT_STATE.yaml`, regenerate the projection, and rerun validation.
4. Workers hand back exact branch/head, changed files, tests/CI, reviews/threads, blockers, and safety boundaries. HQ remains responsible for integration sequencing.
5. `PROJECT_STATE.yaml`, a chat prompt, or a worker handoff never grants merge, `main`, release, deployment, #325, or LIVE authority by itself.

These rules are encoded in the machine-readable `handoff` mapping and validated exactly.

## Updating workstreams

Classify a workstream from live GitHub state, not from copied roadmap text. Use only the versioned status and merge-policy enums accepted by the validator. `current_sha` and `accepted_sha` are optional and should be used only where the identity has a clear non-self-referential meaning.

Do not close, delete, relabel, or rewrite historical Issues/PRs/branches merely to simplify the index. Frozen history remains history; active/blocking surfaces remain represented by their live identities.
