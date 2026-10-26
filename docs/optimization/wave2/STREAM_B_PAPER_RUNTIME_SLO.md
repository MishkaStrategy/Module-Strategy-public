# Optimization Wave 2B — Paper Runtime End-to-End SLO

## Evidence identity

- Repository: `MishkaStrategy/Module-Strategy`
- Branch: `optimization/wave2-paper-runtime-slo`
- Draft PR: `#314`
- Base branch: `optimization/wave2-routing-signal-slo`
- Exact accepted upstream head: `2c4c2e0c4aba1eb8ce45408fc7a15711f81a5429`
- Frozen handoff: `reports/optimization/wave2/stream-c-handoff-b.json`
- Contract version: `wave2c-routing-signal-v2`
- Evidence class: deterministic self-hosted paper-only measurement; not a production capacity claim

## Frozen upstream constraints

Stream B consumes the synchronous and deterministic Stream C contract without reinterpretation:

- Router is stateless and owns no delivery queue or retry state;
- Signal delivery remains synchronous;
- total retained identity capacity is 100,000;
- pending/retry capacity is 10,000;
- `StateCapacityError(resource, current, limit)` fails closed;
- duplicate suppression precedes capacity rejection;
- retry preserves semantic identity;
- terminal evidence is not automatically evicted;
- acknowledged loss, duplicate delivery and ordering violations are zero-tolerance outcomes.

No Router, Signal Bot, Core, Position/Exit or shared DTO contract was changed by Stream B.

## Ownership and measured topology

Stream B owns:

- `modules/paper_execution/**`;
- `applications/trading_runner/**`;
- Stream B-specific tests, measurement harnesses and documentation.

The measured chain is:

`Router result -> Paper admission -> execution intent journal -> Core paper venue -> order projections -> fills -> protection/OCO -> factual position state -> Runner factual routing -> persistence -> checkpoint -> restart recovery -> scoped reconciliation -> readiness -> terminal evidence`

The harness is deterministic, no-network and paper-only. It measures scales 1, 100, 1,000 and 10,000 for Router-to-Paper admission and factual routing, plus scales 1, 100 and 1,000 for isolated completed Paper lifecycles.

## Semantic acceptance invariants

The following outcomes are mandatory:

- one accepted TradePlan creates no more than one Paper intent and one order lifecycle;
- exact duplicate plans, orders, fills and factual events produce no duplicate side effects;
- retry preserves plan, order and fill identity;
- upstream capacity failure produces no Paper admission side effect;
- rejected or unauthorized input produces no order, fill or position;
- live execution remains unavailable;
- protection, cancellation and reconciliation semantics survive restart;
- partial persistence either rolls back or remains explicitly recoverable;
- restart does not create a duplicate fill or duplicate position;
- equal inputs produce equal per-workload semantic digests.

## Correctness defect repaired before performance optimization

The initial smoke profile reproduced a restart defect in `paper_execution.router_execution._load_router_evidence`: it read a nonexistent envelope attribute instead of the canonical aggregate identity retained in the protected journal payload.

The minimal correction reads the journal payload identity while preserving correlation, symbol, hash and conflicting-evidence checks. A direct regression covers accept -> snapshot -> restore -> Router adapter reconstruction -> duplicate replay with zero additional order side effects.

## Baseline breach

The serialized full baseline completed on head `b00fd35e3de8daf230a659f56b1d0efe903e7ac1`.

- Workflow/job: `30754675908` / `91514702826`
- Artifact: `8835882167`
- Artifact ZIP digest: `sha256:4bd8ebb80112c73ddd481eeb5695e482d7e6bee319de15ddbca7a8cc7664e45d`
- JSON SHA-256: `135e5bfdbe762777a66201dd49c22a11bb545fc22b179de3022c2b18575895d8`
- Evidence digest: `c8b44843d3d6f96387c12cbed7e8fe66e4b3638c17b8c158dc201f920311a0e0`
- Full elapsed: `1737.795287 s`
- Peak RSS: `329,519,104 bytes`
- Open FDs: `15 -> 15`

Material baseline results:

| Workload | Throughput | p99 | Additional evidence |
|---|---:|---:|---:|
| Router -> Paper, 1,000 | `58.693 plans/s` | `39.625127 ms` | restart/reconcile `1066.689154 ms` |
| Router -> Paper, 10,000 | `15.254 plans/s` | `318.567523 ms` | restart/reconcile `14811.04543 ms` |
| Factual routing, 1,000 | `112.760 events/s` | `21.126116 ms` | `92.372 bytes/event` |
| Factual routing, 10,000 | `10.373 events/s` | `295.918716 ms` | `92.037 bytes/event` |
| Completed lifecycle, 1,000 | `530.567 lifecycles/s` | `2.63849 ms` | duplicate mutations `0` |

All semantic zero-tolerance counters were zero in the baseline.

## Reproduction and profiler evidence

Two additional comparable 1,000-scale repeats reproduced the degradation. cProfile isolated two Stream B-owned hot paths:

1. factual routing performed three complete mutable-state serialization/hash/save operations per event while the state contained the complete processed-identity map;
2. Paper admission repeated exact Router-envelope inspection and unconditionally materialized all pending snapshots and accumulated order scope state.

The profile did not justify changes to upstream Router generation, Core execution, Position/Exit or public DTOs.

## Minimal remediation

### Trading Runner factual routing

- introduced checkpoint schema v2;
- retained the append-only runtime journal as the authoritative processed-identity ledger;
- kept only `processed_count` and at-most-one pending transaction in mutable checkpoint state;
- retained schema v1 loading and migration on the next event;
- added fail-closed journal/count/integrity validation;
- preserved duplicate suppression, identity-conflict rejection and crash recovery.

### Paper admission

- reused one exact Router inspection during a synchronous admission;
- avoided pending snapshot materialization for plans with no cancel intent;
- cached active scope traversal only for the synchronous preflight duration;
- retained full identity lookup and original cancellation semantics;
- restored original coordinator and venue objects after the fast path.

Regression tests cover schema v1 -> v2 migration, duplicate suppression after restart, terminal-journal/final-checkpoint crash windows, retry after rollback, count/journal mismatch, Paper scope isolation, no-cancel snapshot elision and runtime-object restoration.

## Identical after-profile evidence

The identical serialized full after-profile completed from marker-free source head `c6b9b117ab83148359168aa60b0fdc3568366e33`; the temporary trigger head was `2da4ef3e75af8f370a2a2a014f0ec9d2c3cf4ddd`.

- Workflow/job: `30758890851` / `91525794544`
- Artifact: `8836953315`
- Artifact ZIP digest: `sha256:2762a5ab6f6b650798ff216ec53262f893fc33633c1b9d66f1c7404b218b1f06`
- JSON SHA-256: `bcef4c9462e352f9680f72ccb0704f37a417f35555d3ca80cf245c31b2965976`
- Evidence digest: `ad94365c492ba3b64b8b3de536a5875a588354c278a6364668d5130e45409d78`
- Environment: CPython `3.12.13`, macOS arm64, `PYTHONHASHSEED=0`
- Full elapsed: `512.787613 s`
- Peak RSS: `414,384,128 bytes`
- Open FDs: `16 -> 16`

### Before/after

| Workload | Baseline | After | Change |
|---|---:|---:|---:|
| Full elapsed | `1737.795287 s` | `512.787613 s` | `3.389x`, `-70.49%` |
| Router -> Paper 10k throughput | `15.254/s` | `104.384/s` | `6.843x` |
| Router -> Paper 10k p99 | `318.567523 ms` | `84.9535 ms` | `-73.33%` |
| Router -> Paper 10k restart/reconcile | `14811.04543 ms` | `6441.911833 ms` | `-56.51%` |
| Factual routing 10k throughput | `10.373/s` | `26.569/s` | `2.561x` |
| Factual routing 10k p99 | `295.918716 ms` | `74.636417 ms` | `-74.78%` |
| Completed lifecycle 1k throughput | `530.567/s` | `1067.655/s` | `2.012x` |
| Completed lifecycle 1k p99 | `2.63849 ms` | `1.206 ms` | `-54.29%` |

The after-profile passed compileall, Ruff format, Ruff lint, strict MyPy, component tests, package build, clean-wheel import and paper-only source validation.

## Semantic parity verdict

Semantic parity is exact:

- every Router-to-Paper semantic digest matches the baseline at 1, 100, 1,000 and 10,000;
- every factual-routing semantic digest matches the baseline at 1, 100, 1,000 and 10,000;
- every completed-lifecycle semantic digest matches the baseline at 1, 100 and 1,000;
- acknowledged loss: `0`;
- duplicate fills: `0`;
- duplicate positions: `0`;
- ordering violations: `0`;
- unauthorized admission: `0`;
- live execution: `0`;
- capacity-failure Paper side effects: `0`;
- open-file-descriptor growth: `0`.

The aggregate evidence digest differs because timing, environment/resource measurements and the implementation version are intentionally part of the evidence document; the stable per-workload semantic digests are identical.

## Proposed self-hosted Paper Runtime SLO

These limits apply to the deterministic Stream B full profile on the documented self-hosted arm64 environment. They are regression gates, not production capacity promises.

| Gate | Proposed limit | After-profile | Verdict |
|---|---:|---:|---|
| Full profile elapsed | `<= 600 s` | `512.787613 s` | PASS |
| Router -> Paper 10k throughput | `>= 100 plans/s` | `104.384 plans/s` | PASS |
| Router -> Paper 10k p99 | `<= 100 ms` | `84.9535 ms` | PASS |
| Router -> Paper 10k restart/reconcile | `<= 7,000 ms` | `6441.911833 ms` | PASS |
| Factual routing 10k throughput | `>= 25 events/s` | `26.569 events/s` | PASS |
| Factual routing 10k p99 | `<= 100 ms` | `74.636417 ms` | PASS |
| Completed lifecycle 1k throughput | `>= 500 lifecycles/s` | `1067.655/s` | PASS |
| Completed lifecycle 1k p99 | `<= 3 ms` | `1.206 ms` | PASS |
| Peak RSS | `<= 512 MiB` | `414,384,128 bytes` | PASS |
| FD delta | `0` | `0` | PASS |
| Paper state at 10k | `<= 1,700 bytes/plan` | `1601.062 bytes/plan` | PASS |
| Factual state at 10k | `<= 100 bytes/event` | `92.037 bytes/event` | PASS |
| Semantic zero-tolerance counters | all `0` | all `0` | PASS |
| Per-workload semantic digests | exact baseline match | exact match | PASS |

## Residual limits and non-goals

- Peak RSS increased relative to the baseline but remains below the explicit 512 MiB profile bound; future changes must keep the bound and zero FD growth.
- Factual routing still includes append-only journal cost and is intentionally not converted to an asynchronous queue.
- The 10,000-scale restart/reconcile path remains linear in retained Paper state but passes the proposed bounded recovery SLO.
- No claim is made about multi-process, distributed, live-exchange or production deployment capacity.
- No upstream contract, economic rule, live execution surface, deployment setting or production credential was changed.

## Current verdict

`ACCEPTANCE CANDIDATE — REMEDIATION AND MEASUREMENT COMPLETE`

The full baseline, repeated breach, profiler evidence, minimal remediation and identical after-profile are complete. The temporary full marker has been removed. Final authority requires green marker-free exact-head Actions for the final documentation head and a final unresolved-review-thread check. PR #314 must remain Draft.