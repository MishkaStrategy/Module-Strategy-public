# Full-RC Acceptance Checkpoint — 2026-08-04

Status: **VALIDATION IN PROGRESS — PAPER-ONLY FULL-RC AFTER OPTIMIZATION WAVES 1–2**

This checkpoint supplements the canonical
`docs/integration/FULL_PROJECT_INTEGRATION_LEDGER.md` after the accepted
Optimization Waves 1–2 candidate was transferred into the full-RC branch.
It does not authorize a merge to `main`, a Ready transition, auto-merge, package
publication, deployment, private exchange access, order placement, real portfolio
mutation or live execution.

## History-preserving transfer

- repository: `MishkaStrategy/Module-Strategy`
- source Draft PR: `#306`
- source branch: `integration/optimization-wave-1`
- accepted repaired source head: `f297f6163bc0503a4d5ab5c591fb9a04fa301764`
- previous full-RC head: `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- target branch: `integration/modular-strategy-full-rc`
- relation before transfer: source ahead by 780 commits, behind by 0
- merge base before transfer: exactly `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- transfer method: non-force fast-forward
- rebase: not used
- squash: not used
- force-push: not used
- PR merge button: not used

GitHub automatically recorded PR #306 as merged when its entire head became
contained in the target branch. The branch movement itself was the authorized
history-preserving integration operation.

## Exact-head policy

This document intentionally does not self-embed the commit that contains it.
PR #265 workflows must resolve the live pull-request head, check out that exact
commit, fence it against `integration/modular-strategy-full-rc`, and bind all
published evidence to that identity. Any later commit invalidates prior evidence
and requires another exact-head cycle.

PR #265 remains open, Draft and unmerged. `main` remains outside the scope of this
checkpoint. Current `main` drift and the overlapping README/market-data SVG work
must be resolved separately and history-preservingly before any future merge.

## Preserved canonical strategy identity

- candidate: `strategy-candidate-51659c6ed582f2843357bd33`
- composition hash:
  `51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9`
- composition: FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short
- semantic digest contract: `canonical-strategy-v2-result-v1`
- semantic digest:
  `63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb`
- trades: 438
- final equity: `15266.31855203826`
- net return: `0.526631855203826`
- profit factor: `1.328366621740268`
- maximum closed-equity drawdown: `0.1164174394297347`
- win rate: `0.3515981735159817`

## Preserved registered dataset identity

- dataset: `binance_usdt-m-perpetual_btcusdt_5m`
- version: 1
- dataset hash:
  `49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5`
- source URI: `dataset://binance_usdt-m-perpetual_btcusdt_5m/v1`
- rows: 721,597
- canonical interval: `2019-09-08T17:55:00+00:00` through
  `2026-07-19T06:55:00+00:00`
- relation: `PROVEN_BY_EXACT_FRAME_AND_RESULT_PARITY`

Raw market data, private storage paths and credentials must not be published.
Production datasets must not be mutated. Live trading remains false.

## Required current-head acceptance

The final full-RC exact head must complete and classify:

1. the complete affected project-wide PR matrix;
2. Fast checks, Core CI and Backtest Engine on supported Python versions;
3. full-RC release-readiness with live-head fencing and sanitized evidence;
4. persistent registered-dataset → Backtest Engine smoke v2;
5. registered-dataset canonical Variant 2 parity;
6. canonical Variant 2 backtest;
7. heavy workflows sequentially, never concurrently on the constrained runner;
8. startup-only, skipped, cancelled and queued results without counting them as
   substantive PASS.

Only after exact-head green evidence may PR #265, issue #286 and issue #320 be
updated with current run IDs, artifact IDs and digests.

## Governance boundary

An independent human GitHub `APPROVE` on the final exact head remains mandatory.
Automation, the repository owner account and historical comments cannot substitute
for it. This checkpoint does not authorize Ready, merge to `main`, release,
deployment, private exchange access or live execution.
