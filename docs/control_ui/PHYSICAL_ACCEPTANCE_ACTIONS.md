# Physical acceptance through GitHub Actions

The canonical physical acceptance entrypoint is `.github/workflows/physical-mac-acceptance.yml`.
It replaces the operator loop of downloading a new `module_strategy_physical_acceptance_mac_v1.0.x.sh`
after every accepted repair.

## Automatic trigger

The workflow listens to tree-changing pull-request events against
`integration/modular-strategy-full-rc`, but its heavy jobs run only when the PR head is the
same-repository branch `control-ui/unified-control-plane-foundation`. In the current integration
model this means every accepted history-preserving resync of PR #371 automatically validates the
new exact candidate.

`workflow_dispatch` is retained as an optional exact-SHA interface for the future. The current
acceptance cycle does not depend on manual dispatch because GitHub only guarantees dispatch for a
workflow present on the repository default branch, and `main` is intentionally not modified by
this integration work.

## Jobs

1. `physical-historical` — `[self-hosted, backtester]`. It requires Darwin/arm64 at runtime,
   discovers exactly one approved synced Google Drive corpus root, constructs a temporary two-file
   dataset view, and runs the canonical `prepare`, deterministic `smoke`/restart-replay and `full`
   historical suite.
2. `control-plane-recovery` — `[self-hosted, fast]`. It runs the typed Control Plane CLI/API
   acceptance and the owner-layer System Recovery checks.
3. `dashboard-paper` — `[self-hosted, fast]`. It runs TypeScript/unit/build gates and real-loopback
   Chromium PAPER Runtime E2E with one-time masked credentials.
4. `final-summary` — `[self-hosted, fast]`. It records the job conclusions and emits the residual
   manual checklist. It fails unless all automated jobs passed.

## Evidence and privacy

Raw market data never enters GitHub artifacts. The historical job uploads only sanitized JSON/text
metadata and result hashes. Local corpus/workspace/home paths are redacted before logs are printed
and are rejected from publishable evidence. PAPER logs are scanned for generated tokens and privacy
canaries before upload.

## Safety boundary

This workflow is PAPER/research-only. It does not authorize live trading, private exchange access,
release, deployment, `main` mutation, or the validation-only PR #325 merge. Browser Backup/Restore
remains intentionally unclaimed while #778 lacks an authoritative named server-side recovery
profile. The workflow also does not claim the final 24-hour acceptance cycle.
