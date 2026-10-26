# Integration Project-State Snapshot Semantics

For current machine-readable integration/HQ state, use repository-root `PROJECT_STATE.yaml` and validate it with `scripts/project_state.py`.

`docs/hq/PROJECT_STATE.generated.md` is the deterministic human projection. It is generated from the YAML and is not independently authoritative.

The historical checkpoint values in `FULL_PROJECT_INTEGRATION_LEDGER.md`, `docs/hq/ROADMAP_2026-08-12.md`, Issue bodies, and PR descriptions describe the GitHub state that existed when those records were written. They do not override a newer live-verified `source_checkpoint` in `PROJECT_STATE.yaml`.

A source checkpoint deliberately names the observed integration/main commits rather than the commit that contains the state file. The containing commit cannot truthfully embed its own SHA. Consumers must therefore interpret checkpoint SHAs as observation/source identities, refresh them only after live GitHub verification, and never synthesize a newer current SHA from old prose.

## Bounded open-surface snapshot

The machine-readable state records the complete set of open PR and Issue numbers observed at the checkpoint and classifies each as `active`, `validation-only`, `frozen-history`, `superseded`, or `external-approval`.

That numeric classification is a governance index, not a replacement Issue tracker. Titles, bodies, reviews, CI results, and exact live state still come from GitHub. If the live open count or identity set changes, the recorded classification is stale and must be rebuilt from live GitHub rather than patched by inference.

## Historical branch disposition

`branch_cleanup` is proposal-only. The v1 state contains no delete candidates. Its retained historical branches have exact ancestry evidence proving their recorded heads are ancestors of the canonical integration checkpoint. This is sufficient to classify them as frozen history; it is not authority to delete them.

## Parallel-work handoff

Workers/chats must live-read GitHub before work, maintain one owner per workstream, avoid concurrent mutation of the same branch, and hand back exact head/test/review evidence to HQ. A stale snapshot requires re-verification and regeneration. Neither the snapshot nor a handoff grants merge, `main`, release, deployment, PR #325, or LIVE authority.

No generated summary, roadmap, ledger, workflow artifact, Actions status, or browser state becomes a second project-state owner. GitHub live objects remain the evidence used to refresh the canonical snapshot; the versioned YAML is the repository representation of that verified snapshot.
