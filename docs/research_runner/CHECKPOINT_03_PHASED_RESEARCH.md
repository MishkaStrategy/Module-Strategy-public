# Research Runner checkpoint 03 — phased research and untouched-test control

## Delivered surface

Checkpoint 03 turns the deterministic foundation and durable scheduler into a phased
research orchestrator.

- Initial plans register train jobs only, or `optimization_000` jobs for staged search.
- Staged search performs successive halving over strictly increasing prefixes of the train
  period. Every later optimization stage is activated atomically from successful candidates
  of the immediately preceding stage, under a strictly smaller frozen candidate limit.
- Validation, test, and walk-forward OOS jobs are generated from templates and activated
  atomically against an immutable set of configuration hashes.
- A conflicting second activation for the same target role fails closed.
- Candidate promotion requires successful completion of the prerequisite role.
- Final untouched-test access uses a one-time durable grant bound to experiment,
  specification, split, and candidate fingerprints.
- Ranking consumes externally supplied metrics and applies explicit weighted objectives
  and hard constraints. It does not derive financial metrics.
- Result envelopes are versioned references for Results Aggregator ingestion.
- A fail-closed package sink verifies the authoritative Backtest Engine artifact manifest,
  copies the complete declared evidence set into a separate content-addressed directory, and
  emits the exact Results Aggregator v1 artifact-package manifest without editing Engine output.
- IID bootstrap, moving-block bootstrap, Monte Carlo permutation/block-permutation, and
  random signal-skip plans are deterministic, semantic-seed based, and separate from
  metric calculation.
- Local scheduling supports bounded threads and spawn-based processes, durable leases,
  heartbeat, retries, recovery, cancellation, and artifact verification on resume.
- CLI supports safe specification validation, plan summaries, and durable status reads.

## Anti-leakage invariants

1. No validation, test, OOS, or final job exists in the initial run set.
   Staged plans contain only `optimization_000` jobs initially.
2. A target stage receives only candidate hashes frozen before that stage is executed.
3. Test/OOS candidates must have completed validation successfully.
4. Final candidates must have been frozen before test/OOS and completed that role.
5. A final grant is consumed in the same SQLite transaction that registers final jobs.
6. Candidate hashes, seeds, run IDs, and plan fingerprints are independent of worker or
   completion order.

## Verified evidence

Local checkpoint evidence on Python 3.13:

- 159 Research Runner tests are collected; 158 pass locally and the authoritative Results
  Aggregator import test is skipped only when that separate feature branch is unavailable;
- 92% branch-aware package coverage, above the 90% gate;
- 2 manual high-volume stress tests pass;
- the adapter accepts the real `RunOutcome` type from the authoritative Backtest Engine
  branch;
- 114 adapter and authoritative Backtest Engine tests pass together;
- 244 core/integration, 84 Session Range, 80 Market Regime, and 70 FVG regression tests
  remain green;
- a real output written by the authoritative Backtest Engine is accepted by the registry
  package boundary without changing the Engine artifact manifest;
- the `0.3.0` wheel installs into an isolated target and validates the shipped example;
- latest local wheel SHA-256: `56aca48cbdff5416c602b63a10fa21b11a4534ade8010c85a6676b67e419404e`.

Ruff and strict Mypy remain mandatory CI gates. They could not be executed in the local
container because its internal package index did not provide those tools.
