# Optimization Wave 2A — Research Platform inventory

## Exact dependency and ownership

- Parent stream: Wave 2F Strategy Governance and Canonical Composition.
- Parent accepted head: `c3eea068ff1d04c569ac28807102fc4f7918d610`.
- Stream A branch: `optimization/wave2-research-platform-slo`.
- Draft PR: #311.
- Owned production surface: `research/experiment_runner/**` and only directly required
  integration under `modules/results_aggregator/**`.
- This measurement checkpoint changes no production runtime implementation and no Results
  Aggregator implementation. It adds Stream A benchmarks, contract tests and evidence only.
- Read-only inputs: `research/strategy_audit_validation/**` and
  `reports/strategy-governance-wave2f/**`.

Active Wave 2C PR #309 and Wave 2D PR #310 do not change the Stream A paths. Issue #312 owns
materialization of the missing canonical strategy contracts. Stream A does not infer or
implement those contracts.

## Consumed Stream F contracts

The benchmark verifies every SHA-256 entry in
`reports/strategy-governance-wave2f/evidence-checksums.json` before accepting the handoff.
It consumes:

- workload contract `strategy-governance-synthetic-v1`;
- candidate `strategy-candidate-954bb639db1fc250d14584e1`;
- composition hash `954bb639db1fc250d14584e1abfb802ccb482c12840f29812b3f22256c427b04`;
- the incomplete canonical-composition gap asset;
- the module-contract registry and baseline evidence.

The test loads the gap through `load_composition(..., require_canonical=False)` to verify its
stable identity, then proves that `require_canonical=True` rejects it. No canonical strategy
execution is claimed.

## Research Runner topology

| Boundary | Existing implementation | Synchronous/asynchronous behavior |
| --- | --- | --- |
| Candidate intake | Strict `ResearchSpecification`, frozen mappings and content-addressed configuration IDs | Synchronous validation, no network |
| Experiment identity | Specification fingerprint plus Backtest, metric-schema and Module API versions | Deterministic synchronous hashing |
| Planning | `ResearchPlanBuilder`, grid/random/staged plans and chronological splits | Synchronous and deterministic |
| Durable state | `SqliteExperimentRepository` | Short SQLite transactions and durable state events |
| Queue | Durable run rows with `PLANNED`, `QUEUED`, `RUNNING`, retry and terminal states | Poll-based local queue, not asyncio |
| Claims | Worker/session/token/revision fenced leases | Durable claim and heartbeat boundary |
| Workers | `LocalResearchScheduler` | Bounded thread pool or spawn process pool |
| Retry | Typed `RetryPolicy` and failure taxonomy | Bounded attempts and durable retry wait |
| Timeout | Backtest/worker exceptions classified as transient or terminal | No success conversion |
| Cancellation | `CancellationToken` linearized against publication | Pending and active claims become canceled |
| Restart | Expired leases recovered by repository contract | Stable run identity and sorted final ordering |
| Backtest boundary | `ModularBacktestEngineAdapter` | Delegates to authoritative Backtest Engine |
| Result verification | Closed output manifest with bounded files, sizes, hashes and no symlinks | Synchronous fail-closed verification |
| Result envelope | `RunResultEnvelope` and immutable references | No metric recomputation |
| Aggregator package | `LocalResultsAggregatorPackageSink` | Copies verified evidence into a content-addressed package |

The scheduler has no invented event loop, distributed queue or remote worker service. Process
workers are supported by the product, but the ordinary Wave 2A fast-runner baseline uses the
supported thread model to avoid representing subprocess startup as Backtest capacity.

## Results Aggregator topology

- `SQLiteRegistry` is an immutable, append-audited SQLite/WAL registry.
- `register_package()` validates schema, lineage and references before atomic registration.
- Identical content is idempotent and counted as duplicate entities.
- Same identity with different content fails closed: the original entity remains visible, the
  conflicting entity does not become visible, and the append-only audit/quarantine revision
  advances to record the rejection.
- Artifact metadata is content addressed; artifact code is never executed.
- Registry instances are thread-owned. This benchmark measures sequential publication;
  concurrent publication is not claimed as a supported same-instance topology.
- Query, backup/rebuild, retention and cache layers remain outside the measured publication
  critical path.

## Artifact and resource boundaries

The Runner writes one experiment state database and per-run output directories. The benchmark
records output bytes, total temporary bytes, registry bytes, peak process RSS, available file
descriptor growth, active-child delta and remaining `*.tmp` files. Temporary roots are deleted
by the harness after the report is serialized.

The harness publishes no raw market rows. Dataset binding is a frozen fixture identity only:
`fixture-btcusdt-5m-governance-v1`.

## Existing semantic coverage retained

The authoritative suites already cover:

- repository idempotency and conflicting provenance;
- fenced claim, heartbeat and stale-worker rejection;
- retry ordering and terminal failure taxonomy;
- expired-lease recovery;
- pre-existing and active cancellation;
- spawned process workers;
- output-manifest missing, extra, modified, oversized, special and symlink artifacts;
- partial/atomic SQLite schema creation and migration;
- Results Aggregator missing references, schema/version mismatches and immutable conflicts;
- duplicate registry package import;
- backup, restore, rebuild and corruption guards.

Stream A adds cross-component assertions for Stream F checksums, canonical fail-closed behavior,
meaningful identity changes, deterministic replay digests, duplicate suppression, invalid
admission before side effects, immutable conflict visibility, restart ordering and cancellation
cleanup.

## Benchmark workloads

The measurement harness is
`research/experiment_runner/benchmarks/wave2a_research_platform_slo.py`. The canonical evidence
entry point is `research/experiment_runner/benchmarks/wave2a_verified_baseline.py`, which keeps
scratch data under the symlink-free workspace and verifies the persisted entity after an
immutable publication conflict.

It runs:

- candidate scales 1, 10, 100 and 1,000;
- sequential and bounded four-worker scheduling;
- repeated identical admission;
- mostly-invalid admission;
- deterministic transient retry;
- one 1 MiB result artifact and multiple 1 KiB artifacts;
- Results Aggregator publication at 1, 10, 100 and 1,000 packages;
- duplicate publication and immutable conflict quarantine;
- interrupted lease recovery and continuation;
- cancellation cleanup;
- three comparable 1,000-candidate and 1,000-package repeats.

Measured values include throughput, p50/p95/p99 admission, scheduling, lifecycle,
orchestration-overhead and publication latency, CPU time, peak RSS, FD growth, temporary and
artifact bytes, duplicate/retry/failure counts, recovery and cancellation time, orphan counts
and semantic digests.

## Baseline checkpoint

- Measurement head: `73861889717ab1a656700d778de4d8eec4833ac2`.
- Workflow run: `30750252004`; job: `91502905226`; artifact: `8834237653`.
- CPython 3.12.13, arm64, macOS 27.0, no warm-up, no network.
- Bounded scheduler: 269.30 candidates/s; admission p99 1.9225 ms; scheduling p99
  3624.157 ms; orchestration-overhead p99 20.082583 ms.
- Sequential scheduler: 134.57 candidates/s; scheduling p99 7318.842 ms.
- Results Aggregator: 446.93 packages/s; publication p99 6.611583 ms.
- Three-repeat CV: 5.08% scheduler and 2.03% publication; one semantic digest per workload.
- Peak RSS remained below 85 MB; measured FD growth and orphan process/artifact counts were zero.
- Restart recovery was 0.882041 ms; cancellation cleanup was 4.724625 ms.

## Interpretation boundary

The synthetic engine writes deterministic evidence and processes one fixture bar. Its measured
runtime is separated from scheduler lifecycle time. The result is a local Research Platform
overhead baseline, not a full Backtest throughput claim and not a production capacity claim.
The baseline does not breach the proposed workload-bound SLOs. No production optimization was
performed because there is no repeated SLO violation or profiled Stream A-owned bottleneck.
