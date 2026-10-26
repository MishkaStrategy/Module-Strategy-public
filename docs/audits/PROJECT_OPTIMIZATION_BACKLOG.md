# Project Optimization Backlog

Audit base: `integration/modular-strategy-full-rc` at `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`.

This backlog is intentionally not converted into GitHub Issues automatically. Each item is independently reviewable and must preserve paper-only behavior, deterministic replay, point-in-time correctness and immutable evidence.

## P0 — critical

### OPT-P0-001 — Complete bounded-memory Backtest Engine remediation

**Owner area:** `src/modular_backtest`, `src/modular_core`
**Existing workstream:** Draft PR #287.

**Problem.** A full registered BTC backtest exhausted a 16 GB Mac, reaching approximately 65.2 GB physical footprint and 64.5 GB swap. Static and local measurements confirm multiplicative retention across candles, validation/replay copies, per-bar events, equity, journal indexes, HTF history and checkpoint double execution.

**Implementation direction.**

- Finish `research_fast` bounded observability and keep `canonical_audit` explicitly available.
- Bound base and HTF histories.
- Replace per-bar skip and mark events with deterministic aggregation/sampling where the selected mode permits it.
- Keep append-only event validation without a complete in-memory mirror.
- Add explicit RSS/event/equity/diagnostic guards.
- Separate single-pass research from deliberate checkpoint equivalence runs.
- Follow with true iterator/record-batch input after the first remediation is proven.

**Required evidence.**

1. 10k → 50k → 100k → 250k scale ladder.
2. Peak RSS, physical footprint, swap, GC time, events, equity points and artifact sizes.
3. Exact fills, trades, final equity, reason codes and completed-HTF equivalence.
4. Deterministic restart evidence.
5. Full registered BTC completion under an explicit memory budget.

**Acceptance.** No OOM or swap storm; memory follows configured bounds; canonical evidence remains available; no trading-semantic drift.

### OPT-P0-002 — Define Core bounded-observability contract

**Owner area:** `src/modular_core/journal.py`, event stores, timeframe store.

**Problem.** Core's current journal/event interfaces assume whole-run retention and are the main multiplier behind the Backtest incident.

**Implementation direction.** Define versioned policies for canonical full audit, bounded research and operational telemetry. Make retention limits explicit in manifests and reject ambiguous configuration.

**Acceptance.** Compatibility tests prove exact legacy canonical behavior; bounded mode has finite limits; downstream Result Aggregator and Auditor understand the declared mode.

## P1 — high

### OPT-P1-001 — Stream registered datasets into Backtest

**Owner area:** registered dataset bridge, data feed, Core validation/manifest.

Replace final `list[Candle]`/tuple materialization with ordered record batches or iterators. Compute validation and manifest evidence incrementally. Preserve immutable `dataset_id + version + hash`, gap/duplicate checks, UTC and completed-bar semantics.

**Acceptance.** Same input identity and exact output artifacts; lower peak RSS on 50k/100k/250k/full datasets.

### OPT-P1-002 — Consolidate CI setup and component quality gates

**Owner area:** `.github/workflows`, `.github/actions`.

The exact base produced 50 runs and 84 jobs with approximately 5,213 job-seconds. Many workflows repeat installation, compilation, lint, typing and packaging.

Create reusable quality workflows/actions and phase timing. Keep independent gates parallel but avoid duplicate same-SHA runs. Add narrow concurrency cancellation.

**Acceptance.** No gate removed; at least 30% lower summed job-time on an unchanged-source comparison; no duplicate same-SHA runs caused by overlapping triggers.

### OPT-P1-003 — Pin all third-party GitHub Actions to immutable SHAs

Many workflows use mutable `@v4`, `@v6` or `@v7` refs. Pin every external Action to a reviewed full commit SHA, document upgrade cadence, and retain least-privilege permissions.

**Acceptance.** Routing/security audit rejects mutable third-party refs; all workflows remain green.

### OPT-P1-004 — Normalize mandatory Python runner preflight

Static workflow inspection found approximately 40 jobs not invoking the repository's mandatory reusable Python preflight, although some use legacy shell checks or are non-Python.

Classify every job. Migrate Python jobs to `./.github/actions/python-runner-preflight`; explicitly exempt non-Python/trigger jobs in the policy audit.

**Acceptance.** Zero unexplained violations; resource checks do not report false `0 GiB` on healthy runners; transient disk failures include diagnostics.

### OPT-P1-005 — Harden runner health and capacity reporting

A same-SHA Core run failed because a self-hosted runner reported 0 GiB free, while a parallel run passed.

Add pre-job disk probe diagnostics, mount/path visibility, cleanup verification and optional quarantine after repeated contradictory readings.

**Acceptance.** Reproduction identifies the mount/probe cause; healthy runners no longer false-fail; genuinely low-space runners fail before checkout/install with actionable metrics.

## P2 — medium

### OPT-P2-001 — Optimize Market Data validation/checksum phases

The measured 721,597-row BTCUSDT 5m ingestion took 1,101.233 s. Validation and checksum reread together consumed about 663.45 s.

Profile line-by-line and system I/O, then batch safe validation and remove only provably redundant reads. Do not weaken checksum, atomicity, deduplication or repair behavior.

**Acceptance.** At least 25% lower wall time on the fixed corpus; RSS regression below 10%; identical manifests and hashes.

### OPT-P2-002 — Decompose Market Data orchestration hotspots

Split `IngestionService.execute`, `_repair_interval_gaps`, `validate_sorted_records` and large operations/storage classes behind current interfaces.

**Acceptance.** Mutation/negative/restart tests unchanged; no performance claim unless benchmarked.

### OPT-P2-003 — Add registry scale benchmarks

Measure immutable registration, duplicate replay, conflict rejection and query latency at increasing dataset/partition counts. Include database size, fsync cost and RSS.

### OPT-P2-004 — Make root test collection reproducible

Duplicate test basenames and optional dependencies prevent a naive single-process root collection.

Adopt `--import-mode=importlib` or package namespaces, document extras and create explicit smoke/unit/integration/regression/nightly commands.

**Acceptance.** One documented root collection succeeds without silently omitting existing isolated suites.

### OPT-P2-005 — Add Research Runner process-serialization benchmark

Measure 1/2/4 process workers, payload bytes, queue depth, per-worker RSS, startup cost and deterministic output. Do not increase concurrency until measured.

### OPT-P2-006 — Add Results Aggregator growth benchmark

Measure evidence package validation, SQLite registration, duplicate/conflict paths and query latency as experiment count grows.

### OPT-P2-007 — Add Paper Execution and Trading Runner operational SLOs

Capture event-loop lag, order/fill throughput, reconciliation duration, checkpoint duration, queue depth and long-soak memory.

### OPT-P2-008 — Refactor Trade Plan Router complexity with golden replay

Break high-branch routing methods into named deterministic rules. Verify bit-for-bit plans, reason codes and state transitions on a golden corpus.

### OPT-P2-009 — Refactor FVG checkpoint restoration

Split the large `restore_state` parser/validator without changing checkpoint compatibility. Add legacy checkpoint goldens and mutation tests.

### OPT-P2-010 — Define dependency constraints/lock strategy

Keep library metadata ranges, but generate reviewed CI constraints for Python 3.12 and 3.13. Cache wheels and build environments. Re-run online audit/SBOM.

### OPT-P2-011 — Separate CI setup cost from component runtime

For Wave, Liquidity, Strategy Audit and other long workflows, publish checkout/preflight/install/lint/type/test/build durations independently. Optimize setup only where it dominates.

## P3 — low

### OPT-P3-001 — Consolidate tiny duplicated helpers opportunistically

Only two small exact-clone groups were found: three SQLite connection helpers and two risk `_minimum` helpers. Consolidate only during adjacent work.

### OPT-P3-002 — Archive stale branches and historical workstreams

After governance approval, label/archive obsolete branches and issues without deleting history. Maintain one current performance ledger.

### OPT-P3-003 — Keep bounded analytical module benchmarks as regression gates

Session Range, Market Regime, FVG, Entry, Wave, Protection/Target, Position Scaling and Position Exit currently have adequate bounded-state evidence. Preserve those gates rather than rewriting them.
