# Codex Integration Instructions

1. Use `integration/modular-strategy-v0.5.0` as the target base.
2. Review Draft PR #23; do not merge automatically.
3. Confirm the PR contains only `.github/workflows/position-scaling-hardening-ci.yml` and `modules/position_scaling/**`.
4. Install Core and the module; run all local quality gates.
5. Register the pipeline in dependency order: planner, adjustment-aware Risk producer, approval, executor consumer.
6. Run the contract scenarios in `HANDOFF.md` and the full repository suite.
7. Resolve or explicitly accept PS-CCR-001 in a separate Risk/Core contract PR. Do not silently alter Core in the module PR.
8. Keep exit priority above additions and factual fill reconciliation authoritative.
9. Preserve schema/version/feature namespaces unless a coordinated migration is approved.
10. Do not label the module production/live ready based solely on passing tests.

Expected verdict before merge: integration-review ready only after self-hosted CI is green and the Risk adjustment contract has an explicit disposition.
