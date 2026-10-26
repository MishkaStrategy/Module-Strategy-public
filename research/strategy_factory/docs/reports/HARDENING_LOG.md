# Hardening Log

The user authorized up to 30 evidence-based rounds. This log records only rounds
actually completed. Unlisted rounds are not claimed.

## Round 1 — Canonical contracts and identities

- immutable, versioned Factory contracts;
- UTC-only timestamps and canonical JSON/SHA-256;
- strict request, manifest, checkpoint and artifact decoders;
- same-ID/different-content conflict detection.

## Round 2 — Dataset integrity and causality

- streaming OHLCV schema/type/order/invariant checks;
- gap, duplicate, interval, history and content-hash gates;
- explicit spot/futures and traded/mark/index boundaries;
- point-in-time instrument profile and regimes.

## Round 3 — Module Catalog and Strategy Grammar

- version/maturity/coverage/evidence filtering;
- feature ownership DAG, duplicate producer and cycle rejection;
- market, direction, timeframe, funding and data compatibility;
- multi-role runtime components without duplicate execution.

## Round 4 — Bounded planning

- deterministic hypothesis identities;
- fixed versus searchable parameters;
- real Cartesian search-space truncation to budget;
- canonical Research Runner specifications and plans.

## Round 5 — Durable Research Runner execution

- submit/status/execute/cancel/resume boundary;
- SQLite submission restore without re-preparing completed experiments;
- lost-confirmation and duplicate-submit idempotency;
- native versus registry configuration identities separated.

## Round 6 — Results publication and registry lineage

- official Backtest Engine execution and manifest verification;
- Research Runner immutable package intake;
- Results Aggregator registration, `REGISTERED` and `DUPLICATE` behavior;
- explicit Core 0.4.0/0.4.2 compatibility without arbitrary 0.4.x acceptance.

## Round 7 — Explainable promotion and control arms

- Pareto ranking without opaque aggregate score;
- candidate constraints and explicit rejection reasons;
- package-seeded adapt/improve policies;
- unchanged baseline control at every promotion stage;
- cross-experiment baseline evidence never activated in a foreign experiment.

## Round 8 — Walk-forward, robustness and holdout isolation

- separate frozen-finalist walk-forward experiments;
- temporal/regime/cost/drawdown/trade-count robustness gates;
- canonical Research Runner `FinalTestGrant` delegation;
- one-time holdout activation with consumed-grant evidence;
- holdout contamination detection.

## Round 9 — Audit, package and paper admission

- hash-bound independent audit decision contract;
- fail-closed unavailable audit port;
- parameter/module/period/lineage validation;
- secret-field rejection;
- paper-only package and structurally disabled live authorization.

## Round 10 — Recovery and application lifecycle

- content-addressed artifacts and typed restore codecs;
- checkpoint/hash-chain validation;
- foreground application create/status/inspect/resume/cancel/export;
- terminal runs are not re-executed;
- budget reservations are idempotent across crash recovery.

## Round 11 — Negative testing and coverage gate

- malformed contracts, YAML/JSON, paths, symlinks and hash drift;
- storage races/collisions and disk/replace failures;
- Backtest config provenance and trusted import allowlist tests;
- branch-aware Strategy Factory coverage above the required 90% threshold.

## Round 12 — Acceptance package 1–25

- all 25 user-requested acceptance scenarios have stable test IDs;
- package-seeded adapt/improve regressions are included;
- full Factory suite, Risk Request Composer suite and compileall pass.

## Round 13 — Permanent cross-version quality CI

- Python 3.12 and 3.13 both pass compile, Ruff and strict MyPy;
- Strategy Factory 589-test and Risk Request Composer 6-test floors are enforced;
- all 25 acceptance scenarios are enforced with no failures, errors or skips;
- branch-aware coverage, Bandit and tracked-source safety gates pass.

## Round 14 — Packaging and clean-wheel verification

- Core, Risk Sizing, Research Runner, Results Aggregator, Risk Request Composer
  and Strategy Factory wheels build from the same checked-out commit;
- all six wheels install into a clean isolated environment;
- package imports, distribution identities and the `strategy-factory` CLI smoke pass.

## Round 15 — Fault injection and restart soak

- 41 corruption, checkpoint, artifact, storage-race, decision-log and control tests pass;
- four crash/restart/resume/idempotency scenarios pass for ten consecutive iterations;
- no repeated iteration duplicates a batch or bypasses durable state validation.

## Round 16 — Bounded performance, memory and safety boundaries

- 10,000 canonical hashes: 6.639900708 seconds (limit: 30 seconds);
- 500 immutable artifact put/get cycles: 1.452631834 seconds (limit: 15 seconds);
- 2,000 durable idempotency cycles: 0.329894666 seconds (limit: 15 seconds);
- traced peak memory: 215,444 bytes (limit: 67,108,864 bytes);
- five paper-only and final-audit fail-closed boundary tests pass.

## Round 17 — Extended cold-process restart soak

- 100 independent Python processes execute the restart suite;
- four crash/restart/idempotency checks run per process, 400/400 total;
- `PYTHONHASHSEED` varies deterministically from 1 through 100;
- elapsed time: 79.718079167 seconds (limit: 1,800 seconds);
- run `30367810860` and artifact digest
  `sha256:682fff620fc8474d4851308128cbf6ced3a84567d4c5f046dc955a6dd0fe8460`.

This is a local self-hosted cold-process soak. It does not replace a longer soak in
the intended staging/deployment topology.

## Current measured evidence

- Strategy Factory: 589/589 PASS on Python 3.12 and 3.13;
- Risk Request Composer: 6/6 PASS on Python 3.12 and 3.13;
- Strategy Factory branch-aware coverage: 91% (threshold: 90%);
- explicit acceptance scenarios: 25/25 PASS;
- permanent quality CI run `30367563503` — PASS on both Python versions;
- hardening run `30367564002` — PASS;
- extended restart soak run `30367810860` — PASS, 400/400 checks;
- Research Runner and Results Aggregator compatibility fixes pass their component
  quality and clean-wheel gates.

## Remaining rounds and release gates

Rounds 18–30 are not claimed. The component remains Draft and paper-only. Remaining
blocking work is external or integration-level: issue #250 must deliver a real
independent Strategy Audit & Validation implementation, paper-runtime admission must
be exercised end to end with that auditor, a longer staging soak must run under the
intended topology, and the final independent release audit must pass. No merge,
release, deployment or live execution is authorized.
