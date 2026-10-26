# Final Independent Audit — Protection & Target 1.1.1

## Audited commit

- local branch: `modules/protection-target-hardening`;
- audited source commit: `3dd8f3b5c10cd68ceed679a09f731e7e52f3daf1`;
- remote baseline: `modules/protection-target` at `789680102439fc5b1eed34e2d950cd06be9e0fa8`;
- integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`;
- Core: `0.4.0`;
- Module API: `1.0`.

The audit was executed from a new clean clone of the local commit, using a new product virtual
environment populated only from freshly built Core and Protection & Target wheels. The test runner
and coverage tools were external to the product environment; the module import path was verified to
resolve from the product environment's `site-packages`, not from the source checkout.

## Independent findings

The first import smoke contained an incorrect audit-harness path substring. The installed module was
correctly located in the clean product virtual environment. The harness assertion was corrected and
the entire independent audit was rerun. This was not a runtime module defect.

The final workflow review found one CI auditability weakness: `detect-secrets scan` produced an
artifact but did not fail the job if the report contained findings. The workflow now parses the JSON
and fails closed. This change does not affect runtime behavior or the Module API.

No unresolved critical, high or medium runtime defect was found in the independently audited module.

## Architecture and responsibility boundary

Passed:

- no Core source change;
- no exchange access, system-time access or hidden I/O;
- no sizing, order placement, portfolio mutation or position-management responsibility;
- real `StrategyModule`, `ModuleManifest`, `MarketView`, `ModuleDecision`, `FeatureStore`,
  `ModuleRegistry` and dependency planner are used;
- feature keys remain namespaced and dependency-driven;
- public import and entry point are stable;
- one instance remains bound to one symbol/timeframe stream.

## Mathematical and temporal correctness

Passed:

- fixed-R and structural targets;
- EntryPlan, fixed-percent and feature invalidation;
- BUY/SELL symmetry and geometry boundaries;
- deterministic Decimal context and canonical serialization;
- NaN, infinity, booleans, oversized numbers and invalid geometry rejection;
- exact completed-bar `FeatureView.timestamp` requirement;
- stale/future data rejection and future-data invariance;
- deterministic structural-feature selection and ambiguity rejection;
- no past decision is recalculated from later data.

## State, replay and recovery

Passed:

- state schema 2 and full state digest;
- compatible migration from version 1 and module 1.1.0 schema-2 checkpoints;
- transactional restore;
- corrupt, partial, oversized, incompatible and resigned-semantic corruption rejection;
- exact duplicate idempotency;
- conflicting duplicate, out-of-order, stream-change and gap behavior;
- checkpoint → restart → restore → replay → continuation;
- deterministic output and state digests;
- bounded processed state of 4,096 entries in the million-event workload.

## Clean-checkout validation

| Gate | Result |
|---|---|
| Python | 3.13.5 |
| compileall | passed |
| Ruff format | passed, 22 files formatted |
| Ruff lint | passed |
| strict mypy | passed, 9 runtime files |
| tests | 232 passed, 0 failed/errors/skipped |
| combined line/branch coverage | 93% (gate 90%) |
| Bandit | 0 findings |
| detect-secrets | 0 findings |
| wheel | built and installed |
| source distribution | built |
| Twine metadata | passed |
| `pip check` | passed |
| public import | passed from clean product `site-packages` |
| entry point | passed |
| Registry discovery | passed |
| checkpoint/restore | passed |

Local `pip-audit` could not query its vulnerability service from the sandbox. This is explicitly not
reported as a passed local gate; the self-hosted workflow retains `pip-audit` as a required CI step.

## Performance

Independent clean-checkout benchmark, 2,000 unique plans, five traced runs:

- minimum: 1.019143 s;
- median: 1.099625 s;
- maximum: 1.255242 s;
- median throughput: 1,818.80 plans/s;
- peak traced memory: 0.573692 MiB;
- median checkpoint: 235,551 bytes;
- median restore: 4.297 ms.

The post-error controlled benchmark recorded 1,832.22 plans/s. Both measurements exceed the local
1.0.0 baseline of 1,631.06 plans/s without meaningful peak-memory growth. The difference between the
two 1.1.1 measurements is treated as host scheduling variance, not as a semantic difference.

Extended evidence also passed:

- 300,000-event workload;
- 1,000,000-event multi-process restart workload;
- aggregate million-event throughput: 7,832.76 events/s;
- final state: 4,096 entries;
- maximum checkpoint: 492,187 bytes;
- repeated 50,000-event runs produced identical output and state digests.

## Packaging and workflow

Passed:

- wheel and sdist include source, `py.typed`, tests, benchmark and documentation as intended;
- built distributions and bulky generated reports are excluded from the source branch and retained as CI artifacts;
- no virtual environments, credentials, caches or market datasets are packaged;
- macOS self-hosted workflow uses the confirmed labels `[self-hosted, macOS, ARM64]`;
- Python 3.12 and 3.13 run serially;
- concurrency cancellation, timeouts, cleanup and 14-day evidence retention are configured;
- security, SBOM, benchmark, clean install and checksum evidence are generated;
- detect-secrets is a fail-closed gate.

## Remaining external validation

- Python 3.12 was unavailable in the local sandbox and must pass on the self-hosted runner.
- `pip-audit` requires the self-hosted runner's vulnerability-index access.
- full monorepo Core/integration jobs must pass after publication.
- no paper/live or exchange-adapter readiness claim is made.

## Verdict

**READY FOR DRAFT PR CI AND INTEGRATION REVIEW.**

The module is locally ready for integration. The PR must remain Draft until the published commit has
passed self-hosted Python 3.12/3.13, packaging, security and monorepo compatibility checks.
