# Wave 2 Stream C — Trade Plan Router and Signal Delivery

## Identity

- Repository: `MishkaStrategy/Module-Strategy`
- Base branch: `integration/optimization-wave-1`
- Base SHA at stream creation: `041ee4466232feab448daa377fb33d7898d50fd9`
- Working branch: `optimization/wave2-routing-signal-slo`
- Draft PR: `#309`
- Stream B handoff contract: `wave2c-routing-signal-v2`
- Technical verdict: `ACCEPTED — REMEDIATION COMPLETE`

This stream remains Draft. It must not be merged, marked Ready, auto-merged,
force-pushed, rebased, released, deployed or connected to live trading without a
separate project decision.

The recorded measurements are deterministic local/self-hosted evidence. They are not a
production transport or exchange-capacity claim.

## Scope and ownership

Owned:

- `modules/trade_plan_router/**`;
- `src/signal_bot/**`;
- `tests/signal_bot/**`;
- Stream C benchmarks, reports and handoff assets;
- this document and PR #309 metadata.

Read-only surfaces were not modified:

- Trading Runner;
- Paper Execution;
- Position/Exit;
- Modular Core and Backtest;
- Strategy Factory and Strategy Audit;
- Market Data, Dataset Registry and LocalStorage;
- shared DTOs/contracts;
- shared workflows and root tooling.

No trading rule, `TradePlan` field, execution authority, live Telegram delivery or live
trading behavior was changed.

## Live recovery

Before implementation the live repository state was checked:

- PR #306 was open and Draft;
- PR #309 was open and Draft on the expected branch and base;
- initial Stream C head was `004e12182d31333cde0ccb2de3f11ce871efa25b`;
- no review submissions or unresolved review threads existed;
- no foreign commit was reverted;
- PRs #308–#310 were checked before each heavy run.

The full baseline and full after-profile start/end records are preserved in PR #309
comments. The one-shot full-profile gate was removed after each evidence capture so a
normal commit cannot accidentally repeat a heavy workload.

## Topology inventory

### Trade Plan Router

The Router is synchronous, deterministic and stateless:

1. `RouterInputPackage` carries portfolio, entry, protection, sizing and scaling inputs.
2. Typed models validate schemas, ownership links, timestamps and deterministic hashes.
3. `DecisionRouter.route()` validates freshness, duplicate decision IDs, linkage and
   incompatible decision sets.
4. Candidate selection is deterministic and configuration-driven.
5. An accepted result contains exactly one immutable executable plan.
6. A rejected result contains no executable plan and typed reason codes.
7. The Router owns no queue, retry loop, acknowledgment state or restart checkpoint.

### Signal Bot

Signal delivery is synchronous with a persistent SQLite outbox:

1. a signal enters `SignalEngine.process()` or `SignalEngine.ingest()`;
2. validation rejects unsupported contracts, identity mismatch, open/stale candles and
   invalid price geometry;
3. `SQLiteStateStore.register()` performs deduplicated admission by canonical
   `signal_id`;
4. accepted signals move through lifecycle states;
5. rendering, formatting and notifier delivery are synchronous;
6. `sent` plus a persisted `message_id` is the delivery acknowledgment;
7. notifier or renderer exceptions become `send_failed` with retry metadata;
8. `retry_pending()` reads eligible rows in deterministic bounded batches;
9. SQLite state restores deduplication, terminal acknowledgments, retry state, monotonic
   cursors and capacity counters after restart.

There is no `asyncio` boundary, background queue or event-loop consumer in contract v2.
Event-loop lag is therefore not applicable.

## Locking, ordering and backpressure

| Concern | Contract |
| --- | --- |
| Execution model | synchronous |
| Router mutable state | none |
| Signal state lock | process-local `threading.RLock` |
| Cross-connection write serialization | SQLite `BEGIN IMMEDIATE` |
| Persistent queue | non-terminal SQLite `signals` rows |
| Recovery fetch | `ORDER BY created_at LIMIT 100` |
| Total retained identity capacity | 100,000 |
| Pending/retry capacity | 10,000 |
| Overflow | fail closed with `StateCapacityError` |
| Dedup identity | canonical `signal_id` / SQLite primary key |
| Terminal retention | preserved; no automatic deletion |
| Equal-timestamp total order | not promised across processes |
| Natural backpressure | notifier wall-clock duration blocks the current producer |

A duplicate lookup occurs before capacity rejection. A previously registered identity is
therefore still suppressed when the store is full rather than being reclassified as a
capacity failure.

## Semantic invariants

The hard SLO is zero tolerance:

| Invariant | Required | Baseline | After |
| --- | ---: | ---: | ---: |
| Duplicate terminal delivery | 0 | 0 | 0 |
| Acknowledged delivery loss | 0 | 0 | 0 |
| Invalid admission success | 0 | 0 | 0 |
| Ordering violations | 0 | 0 | 0 |
| Private payload leakage | 0 | 0 | 0 |

Additional tests freeze:

- deterministic serialization and nearest-rank percentiles;
- retry semantic identity;
- acknowledged state surviving restart;
- terminal delivery not replayed after restart;
- corrupt persisted payload cancellation;
- monotonic cursors under out-of-order input;
- pending overflow failing closed;
- terminal cancellation releasing one pending slot;
- total-cap duplicate suppression;
- counter reconciliation after restart;
- strict capacity configuration;
- two SQLite connections not over-admitting;
- concurrent terminal transition decrementing the pending counter exactly once.

## Benchmark harness

Canonical path:

`tests/signal_bot/wave2_slo_benchmark.py`

Schema version: `2`.

The harness uses no network, Telegram production API, exchange credentials or live
trading.

Full profile:

- steady: 1, 100, 10,000 and 100,000;
- bursts: 100, 1,000 and 10,000;
- duplicates: 0%, 10%, 50% and 90%;
- invalid input: 1%, 10% and 50%;
- retry, slow consumer and failing consumer;
- queue saturation;
- restart with 1,000 recoverable rows;
- 100,000-identity bounded-state soak;
- three repetitions for every workload/component group.

Each capture contains 102 records across 34 workload/component groups with throughput,
CPU, p50/p95/p99/max latency, RSS, persisted bytes, queue depth, dedup/retry sizes,
restart time, failure taxonomy and semantic-loss counters.

The full profile is evidence-only. It is not retained as an ordinary CI test.

## Baseline evidence

- Head: `57539351159a9d99ca90c47dc92785b69e2ad9d6`
- Core CI run: `30745424109`
- Artifact ID: `8832876395`
- Artifact SHA-256:
  `124b489a97c2c12fac5ffc0616e419943a01229b0efdd123d8b6f02e0a9ede07`
- Benchmark JSON SHA-256:
  `d3e24ae9b9afe6d626d107b12821de4b79fabdb9f925d871053088c84b2e0f8e`
- Environment: CPython 3.12.13, macOS arm64, SQLite 3.53.1

The baseline proved a bounded-state breach:

- recovery reads were bounded to 100 rows;
- pending/retry state could grow to 10,000 without an admission contract;
- terminal/dedup state grew to 100,000 identities;
- 100,000 identities occupied 111,746,672 persisted bytes;
- growth was approximately 1,117 bytes per retained identity;
- there was no fail-closed total or pending capacity.

This was a repeatable state-complexity and operability failure, not a speculative
performance observation.

## Minimal remediation

The Signal Bot store now provides:

- default total identity capacity: 100,000;
- default pending/retry capacity: 10,000;
- atomic durable `state_counters`;
- counter reconciliation when an existing database opens;
- `BEGIN IMMEDIATE` around admission and lifecycle mutation;
- `StateCapacityError(resource, current, limit)`;
- duplicate suppression before capacity rejection;
- exactly-once pending-counter release on terminal transition;
- pending-counter release when corrupt payloads are cancelled;
- no automatic deletion of terminal evidence.

No async queue, cache, validation weakening or trading-semantic change was introduced.

## Before/after evidence

- After head: `ffadc5829bcf747e842c7a97adfc0bbf33083dd7`
- Core CI run: `30746786825`
- Artifact ID: `8833297064`
- Artifact SHA-256:
  `d619172a9430c5dbf5535582f8be67913752fc97c0713df382242ad509cd25a6`
- Benchmark JSON SHA-256:
  `ab818479d5913047ba1b9a23ec18e6184659f97ef43213ed809d2ccaa5fb0765`

| Workload | Baseline throughput | After throughput | Baseline mean p99 | After mean p99 |
| --- | ---: | ---: | ---: | ---: |
| Router steady 100k | 1,237.85/s | 1,196.90/s | 1.103 ms | 1.208 ms |
| Router burst 10k | 1,231.59/s | 1,077.57/s | 1.188 ms | 2.813 ms |
| Signal steady 100k | 2,152.35/s | 2,303.84/s | 1.314 ms | 1.814 ms |
| Signal burst 10k | 2,289.37/s | 2,402.65/s | 1.165 ms | 1.132 ms |
| Queue saturation 10k | 2,193.80/s | 2,467.60/s | 0.996 ms | 0.961 ms |
| Restart 1k | 1,417.20/s | 1,554.51/s | 32.625 ms | 32.381 ms |
| Retry 1k | 1,408.36/s | 1,579.14/s | 28.366 ms | 29.606 ms |
| Failing consumer 1k | 2,135.35/s | 2,415.12/s | 1.039 ms | 0.908 ms |
| Soak 100k | 4,452.77/s | 4,142.96/s | 0.635 ms | 1.018 ms |

Restart recovery samples after remediation were 242.168, 281.811 and 265.941 ms.
Peak full-process RSS was 190,103,552 bytes. Persisted size at 100,000 identities was
111,849,144 bytes, an increase of 0.092% for the durable counter contract.

Router production code was unchanged. The after Router burst result contained one local
runner outlier at 732.59 plans/s and 6.477 ms p99; the other two samples were 1,275.43/s
and 1,224.67/s. The Signal soak also contained one outlier; repeats one and three were
faster than baseline. These are reported as variance, not attributed to the SQLite
capacity change.

Machine-readable evidence:

- `reports/optimization/wave2/stream-c-before-after.json`
- `reports/optimization/wave2/stream-c-handoff-b.json`

## Local workload SLO

These limits apply only to the recorded self-hosted environment and workload schema:

| SLO | Threshold | After | Verdict |
| --- | ---: | ---: | --- |
| Router steady 100k mean throughput | >= 1,000/s | 1,196.90/s | PASS |
| Router steady 100k max sample p99 | <= 1.5 ms | 1.298 ms | PASS |
| Router burst 10k mean throughput | >= 1,000/s | 1,077.57/s | PASS with variance warning |
| Router burst 10k max sample p99 | <= 7.0 ms | 6.477 ms | PASS with variance warning |
| Signal steady 100k mean throughput | >= 1,900/s | 2,303.84/s | PASS |
| Signal steady 100k max sample p99 | <= 3.0 ms | 2.949 ms | PASS |
| Signal burst 10k mean throughput | >= 1,800/s | 2,402.65/s | PASS |
| Signal burst 10k max sample p99 | <= 2.5 ms | 2.022 ms | PASS |
| Restart recovery, 1,000 rows | <= 350 ms | 281.811 ms max | PASS |
| Peak RSS | <= 200 MiB | 181.30 MiB | PASS |
| Persisted size, 100k identities | <= 120 MiB | 106.67 MiB | PASS |
| Duplicate delivery | 0 | 0 | PASS |
| Acknowledged loss | 0 | 0 | PASS |
| Invalid admission success | 0 | 0 | PASS |
| Total/pending overflow | fail closed | tested | PASS |

No further production optimization is authorized by this evidence.

## Stream B contract freeze

Machine-readable contract:

`reports/optimization/wave2/stream-c-handoff-b.json`

Version `wave2c-routing-signal-v2` freezes:

- existing `RouterInputPackage` and `RoutingResult` semantics;
- deterministic plan identity and rejection reason codes;
- `sent` plus `message_id` as acknowledgment;
- retry preserving `signal_id`;
- duplicate suppression before capacity checks;
- synchronous delivery and natural producer backpressure;
- recovery order in batches up to 100;
- total capacity 100,000;
- pending/retry capacity 10,000;
- fail-closed `StateCapacityError`;
- counter reconciliation after restart;
- no automatic terminal evidence eviction;
- error taxonomy and benchmark schema v2.

Stream B must handle `StateCapacityError` explicitly and must not assume asynchronous
delivery. Any semantic change requires a version bump and coordinator handoff.

## Validation

Authoritative evidence completed during the stream:

- compileall: PASS;
- Ruff lint: PASS;
- Ruff format: PASS;
- focused strict MyPy: PASS;
- Signal Bot tests on Python 3.12 after remediation: 87 passed;
- Python 3.12 and 3.13 Core CI: PASS;
- v0.6 executable acceptance: PASS;
- wheel build and installed-wheel verification: PASS;
- benchmark smoke: PASS;
- full baseline: PASS;
- full after-profile: PASS;
- semantic and cross-connection capacity tests: PASS.

The final documentation/handoff head must also complete exact-head Actions. Queued or
in-progress checks are not counted as success.

## Acceptance

Technical status:

`ACCEPTED — REMEDIATION COMPLETE`

Acceptance reasons:

1. topology is documented;
2. semantic invariants are executable tests;
3. benchmark schema v2 is reproducible;
4. p50/p95/p99 and variance are captured;
5. persistent admission is bounded;
6. duplicate delivery is zero;
7. acknowledged loss is zero;
8. restart behavior is verified;
9. the unbounded-state bottleneck was reproduced;
10. the minimal change has identical-workload before/after evidence;
11. Stream B contract v2 is frozen;
12. ownership boundaries were preserved.
