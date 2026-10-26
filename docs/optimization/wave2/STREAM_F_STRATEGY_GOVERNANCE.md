# Wave 2 Stream F — Strategy Governance and Canonical Composition

## Identity

- Base branch: `integration/optimization-wave-1`
- Audited base SHA: `041ee4466232feab448daa377fb33d7898d50fd9`
- Working branch: `optimization/wave2-strategy-governance-slo`
- Draft PR: `#308`
- Phase: Wave 2 measurement and governance contract

## Outcome

Stream F created a versioned, deterministic and fail-closed composition/governance
foundation plus reproducible Strategy Audit workloads. It did not change trading
semantics and did not optimize production code.

The exact executable composition named
`FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short` remains unavailable in the
repository. The committed composition asset is deliberately marked `INCOMPLETE`,
non-canonical, non-executable and non-approvable. It is a machine-readable gap record,
not a substitute strategy.

## Canonical composition contract

- schema: `strategy-composition-v1`;
- gap asset:
  `research/strategy_audit_validation/compositions/canonical-fvg-rt-final-15m-frozen-1h-4h.gap.json`;
- composition hash:
  `954bb639db1fc250d14584e1abfb802ccb482c12840f29812b3f22256c427b04`;
- immutable candidate ID:
  `strategy-candidate-954bb639db1fc250d14584e1`;
- state: `INCOMPLETE`;
- exact blockers: `reports/strategy-governance-wave2f/contract-gaps.json`.

The hash is stable for canonical JSON and changes when any meaningful composition
field changes. Approval state is not part of candidate identity and cannot be mutated
by the candidate.

## Governance invariants

The Stream F contract enforces:

- code-owned module registry, exact versions and required parameters;
- UTC-only composition and evidence;
- look-ahead and unfinished HTF candles forbidden;
- exact dependency and execution ordering;
- deterministic composition hash and candidate ID;
- immutable canonical evidence bytes and digest;
- stale evidence rejection;
- detached independent approval records;
- candidate creator and approval actor separation;
- HMAC-signed approval integrity;
- monotonic checkpoint/restart with exact candidate/evidence digest;
- SQLite payload and chain corruption detection;
- fail-closed incomplete canonical composition.

## Measurement harness

Command:

```bash
PYTHONPATH=research/strategy_audit_validation/src \
python research/strategy_audit_validation/benchmarks/strategy_governance_slo.py \
  --workload valid --candidates 1000 \
  --database /tmp/wave2f-valid-1000.sqlite3 \
  --output /tmp/wave2f-valid-1000.json
```

Supported workloads:

- 1, 10, 100 and 1,000 valid candidates;
- repeated identical candidates;
- mostly-invalid candidates;
- 1 MiB evidence payloads;
- interrupted audit and restart;
- corrupted evidence/database state;
- concurrent readers.

The harness uses no network, exchange credentials or live trading.

## Baseline

Reference environment: CPython 3.13.5, Linux x86_64, SQLite WAL with
`synchronous=FULL`.

Primary valid-1,000 sample:

- 217.15 attempts/second;
- cold p50/p95/p99: 4.115 / 12.370 / 22.613 ms;
- warm p95/p99: 4.659 / 11.118 ms;
- peak RSS delta: 10,616,832 bytes;
- artifact growth: 9,182.808 bytes per unique candidate;
- zero failures.

Three additional valid-1,000 samples:

- mean throughput: 269.00 attempts/second;
- coefficient of variation: 6.00%;
- one exact semantic digest across all samples;
- identical RSS delta and artifact size.

Recovery/security observations:

- interrupted-1,000 restart overhead: 0.762 ms with exact final counts;
- repeated-1,000 plus replay: 1,999 duplicates suppressed and one durable identity;
- corrupted workload: `EvidenceTamperedError` detected;
- concurrent readers: 4,000 successful reads, zero failures.

Evidence is under `reports/strategy-governance-wave2f/**` with a SHA-256 inventory.
Raw SQLite databases are not committed.

## SLO decision

The workload-bound proposal is in
`reports/strategy-governance-wave2f/slo-proposal.json`. The three primary samples meet
all proposed thresholds. No reproducible SLO breach or concrete production bottleneck
was established, so speculative production optimization was not performed.

## Validation

Local focused validation completed:

- compileall: pass;
- 70 governance/composition/benchmark tests: pass;
- focused branch coverage for the seven new governance modules: 96%;
- deterministic benchmark repeat: pass;
- restart/recovery: pass;
- corruption detection: pass;
- evidence checksum verification: pass.

Ruff, Ruff format, strict MyPy, package smoke and repository integration remain
substantive GitHub Actions gates on the exact pushed head; queued or in-progress checks
are not counted as successful.

## Stream A handoff

Machine-readable handoff:
`reports/strategy-governance-wave2f/handoff-to-stream-a.json`.

Stream A may consume the governance schema, workload generator, benchmark outputs,
composition hash and gap report. It must not treat the incomplete gap composition as an
executable trading strategy and must not modify Stream F-owned identities or evidence.
Canonical strategy research can start only after all exact module contracts exist and
`load_composition(..., require_canonical=True)` succeeds.

## Safety

Keep PR #308 Draft. No merge, Ready-for-Review transition, auto-merge, force-push,
rebase, release, tag, deployment, credentials, live trading or trading-semantic change
is authorized.
