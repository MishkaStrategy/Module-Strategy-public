# Optimization Wave 1 Validation

## Verdict scope

This report validates the integrated Optimization Wave 1 candidate. It separates evidence produced on source heads, evidence reproduced during conflict integration, and checks that must run on the final Draft PR head.

No figure below is invented. Where an exact final-tree measurement has not yet been executed, the report states that explicitly.

## Identities

- Target branch: `integration/modular-strategy-full-rc`
- Verified target head: `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- Integrated code candidate before documentation: `383e69657777f8bcd8918b1b1fc35740f93f9234`
- Canonical Backtest/Core/streaming source: PR #294 at `bd498ac1e71c8a68cb26a98b56bce7ab5999bf8f`
- Late Backtest recovery delta: PR #287 at `6a60dc0607dd255c4a29dc55bc4c97e96c2c0a5f`
- Validated late-delta merge: `580f12cb3ea7da699cc30b1c97abc485a835f322`

The final Draft PR head includes this report and is therefore newer than the code candidate SHA. Exact-head CI associated with the final PR is the authoritative final validation fence.

## Method

1. Restore live base, source-PR heads, Draft state, changed files, review threads and exact-head workflow runs.
2. Integrate source branches in the prescribed order with ordinary merge commits.
3. Compute live ancestry rather than assuming PR #294 still contains all of PR #287.
4. Resolve repository-wide intersections semantically, not by whole-file selection.
5. Validate the late recovery merge in an isolated GitHub-hosted environment with the exact three-way conflict set asserted before applying resolutions.
6. Preserve benchmark evidence only when its source SHA, corpus identity and methodology are recorded.
7. Keep validation-only and research-only PRs outside the production integration tree.
8. Run final-tree CI on the Draft PR head and record any remaining limitations.

## Validation environments

### Isolated late-delta merge run

GitHub Actions run `30735370347` used:

- hosted Ubuntu runner;
- CPython 3.12.13;
- uv 0.11.31;
- PyArrow 25.0.0;
- Ruff 0.16.1;
- editable root dev/backtest installation;
- editable Historical Market Data Ingestion Parquet installation.

The run asserted exactly three conflicts, applied the reviewed resolution, formatted the changed files, and completed:

- `git diff --check`;
- `python -m compileall` over Backtest/Core tests and the scalability benchmark;
- Ruff check;
- Ruff format check;
- 100 focused Backtest/recovery/registered-streaming tests.

Result: PASS.

### Source benchmark environments

The exact environments are documented in the source PR evidence. Important differences are retained instead of being silently normalized:

- Backtest full-BTC evidence used fresh Linux processes and CPython 3.13.5.
- Ingestion paired evidence used the same input, Python, validation policy and runner per baseline/optimized pair.
- PR #298 strategy evidence used the combined streaming/backtest tree but a non-canonical research baseline.

## Test architecture evidence

PR #289 source-head evidence reported:

- 22 isolated owner suites;
- 6,775 test IDs;
- zero lost IDs;
- zero unmapped test files;
- zero unmapped `conftest.py` files;
- deterministic repeated collection;
- 1,737 optional-free tests passed.

The integrated workflow retains:

- static inventory fail-closed checks;
- optional-free root validation;
- all-suite collection twice;
- exact node-ID comparison;
- audited-base comparison;
- hash-seed and UTC fencing;
- immutable Actions and retained evidence artifacts.

The full all-suite execution is not promoted to a mandatory PR check without runner-duration and load measurements.

## Backtest/Core bounded-memory evidence

### Historical failure baseline

The earlier registered BTC execution retained approximately 65.2 GiB physical footprint and 64.5 GiB swap. This was the confirmed P0 scaling failure addressed by PR #287/#294.

### Full BTC evidence from the current PR #287 source head

Corpus identity:

- BTCUSDT, Binance USDT-M Perpetual, 5m;
- file bytes: 34,020,301;
- file SHA-256: `2b8a20ef09e1c292abbbee4a6963a1cedb7904cfc4340c9de72d0d2cd0b87ed1`;
- canonical candle hash: `afe74d8ece22d748136fd05e6436841fec7a40be686d65e1d700e1eca0a55c52`;
- 721,597 rows;
- first candle: 2019-09-08 17:55:00 UTC;
- last candle: 2026-07-19 06:55:00 UTC;
- zero gaps and zero duplicates.

| Workload | Mode | Wall | Peak RSS | Swap | Orders | Fills | Trades | Result |
|---|---|---:|---:|---:|---:|---:|---:|---|
| deterministic no-op | direct | 70.78 s | 325.7 MiB | 0 | 0 | 0 | 0 | PASS |
| deterministic no-op | runtime snapshot | 80.65 s | 381.3 MiB | 0 | 0 | 0 | 0 | PASS |
| order lifecycle | direct | 79.03 s | 335.0 MiB | 0 | 5,230 | 5,230 | 2,615 | PASS |
| order lifecycle | runtime snapshot | 90.70 s | 374.3 MiB | 0 | 5,230 | 5,230 | 2,615 | PASS |

Direct and restarted digests matched exactly. The lifecycle pair also matched total fees `87.06233946812` and final equity `9902.115903631895`.

The runtime snapshot path performs one trading simulation, validates snapshot schema and hashes, resumes at the source suffix and replays zero prefix rows through broker/strategy execution.

### Integrated recovery compatibility

The late-delta merge preserves four distinct contracts:

- disabled execution;
- legacy deterministic replay for registered materialized rollback mode;
- bounded registered cursor replay for registered streaming mode;
- runtime snapshot restart for ordinary prepared sources.

The shared application metadata now records recovery mode, simulation passes, source traversals, source rows replayed and snapshot fields without assuming every checkpoint type has a runtime snapshot payload.

## Registered streaming evidence

PR #294 source-head evidence used the same 721,597-row BTC corpus and 83 monthly partitions.

### No-op full history

- preparation: 37.638 s;
- execution: 77.338 s;
- throughput: 9,330.5 bars/s;
- peak RSS: 319.3 MiB;
- zero runtime errors;
- canonical result digest prefix recorded in source evidence as `6a4535...`.

### Non-empty lifecycle

- preparation: 37.935 s;
- execution: 79.790 s;
- peak RSS: 321.2 MiB;
- orders/fills/trades: 2/2/1;
- zero runtime errors;
- canonical result digest prefix recorded in source evidence as `8647ec...`.

### Contract coverage retained

The integrated tests cover:

- legacy default compatibility;
- explicit streaming mode;
- batch-size invariance for representative batch sizes including 1, 64, 1,024 and 8,192 in source evidence;
- canonical data hash and result-digest equality;
- cursor continuation and exhaustion;
- partition crossing and selected ranges;
- fail-closed partial/corrupt sources;
- no duplicate or skipped candles;
- source-reader closure on cancellation;
- bounded-memory execution.

PR #297 independently reported a 500,000-row synthetic comparison of 220.3 MiB streaming peak RSS versus 836.8 MiB legacy peak RSS, with time-to-first-batch 2.24 s versus approximately 140 s. PR #297 remains evidence-only and was not integrated.

## Strategy semantic evidence and acceptance blocker

PR #298 exercised an event-heavy BTCUSDT 4H FVG first-retest research strategy over the 721,597-row corpus:

- 940 orders;
- 462 fills;
- 231 trades;
- zero runtime errors;
- 452.2 MiB peak RSS;
- exact legacy/streaming economic equivalence;
- streaming end-to-end overhead approximately 3.65%;
- streaming peak RSS approximately 3.12% lower;
- return -20.42%, profit factor 0.775, drawdown 23.87%, fees 1,186.31 USDT.

This proves event-heavy semantic equivalence but does not validate profitability and is not the canonical project strategy.

Repository search did not find an exact executable YAML/module composition for:

`FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`

Therefore a full canonical-strategy legacy/streaming A/B was not fabricated. The missing executable composition remains an acceptance blocker for strategy-specific Wave 1 sign-off, while engine, streaming, recovery and generic lifecycle validation continue to pass.

## Historical Market Data Ingestion evidence

PR #290 paired real BTC benchmark:

- baseline: 933.991 s;
- optimized: 761.056 s;
- end-to-end improvement: approximately 18.516%;
- decode/spool improvement: approximately 25.863%;
- validation/promotion improvement: approximately 31.078%;
- temporary spool bytes materially reduced;
- peak RSS increased approximately 11.256%;
- typed digest, checksums and partition identity remained equivalent.

Known limitations:

- the 25% end-to-end target was not reached;
- final typed-checksum reread remains the largest I/O stage;
- the complete 12,968,018-row multi-dataset corpus was not rerun for this optimization;
- checksum trust boundaries were not weakened.

The integrated workflows preserve paired baseline/head execution, real BTC source SHA validation, exact row/partition expectations, typed-digest equivalence and artifact retention.

## CI/CD evidence

PR #291 source-head evidence reported:

- mutable Action references reduced from 165 to 2;
- Fast checks approximately 76 s to 41 s;
- preflight approximately 62.6 s to 6.4 s;
- 61 artifact uploads assigned retention policies;
- duplicate workflow paths reduced.

Integration subsequently hardened the new #289/#290 workflows and changed the action-pinning baseline to require zero mutable external Action references. The final baseline contains an empty `allowed_counts` map.

Policies retained:

- immutable external Action SHAs;
- pinned uv manifest/version contract;
- read-only checkout credentials;
- role-based runner routing;
- concurrency cancellation where safe;
- scoped cache identities;
- artifact retention;
- diagnostic uploads that do not mask substantive failures;
- fail-closed workflow contract tests.

Exact final-PR before/after totals for declared jobs, expanded jobs, queue time, artifact bytes and duplicate exact-SHA runs require final PR run completion and are not claimed here before that evidence exists.

## Static quality, security and packaging matrix

The final Draft PR is expected to provide the exact-head authority for:

- compileall;
- Ruff check and format check;
- MyPy for affected packages;
- actionlint;
- workflow policy tests;
- shell syntax;
- build metadata checks;
- Bandit;
- detect-secrets;
- pip-audit;
- SBOM generation;
- wheel/sdist/Twine checks where configured;
- clean package installation paths.

The isolated late-delta run already passed compileall, Ruff check and Ruff format on the conflict-sensitive Backtest surface. Security/packaging checks are not marked final until exact-head CI finishes.

## Diff and artifact hygiene

The integration intentionally excludes:

- raw market data;
- registry data bytes;
- local benchmark output directories;
- private filesystem paths;
- credentials and exchange secrets;
- temporary merge-lab patches/workflows from the production branch;
- PR #297 and PR #298 implementation trees.

The temporary merge-lab branch and Draft PR #305 are not part of the integration ancestry.

## Branch protection and repository rules

The available GitHub connector did not expose repository ruleset or branch-protection administration. No administrative change was attempted.

Recommended minimum restrictions for `main` and `integration/modular-strategy-full-rc`:

- require pull requests;
- prohibit force pushes and deletion;
- require conversation resolution;
- require exact-head actionlint, workflow policy, Fast checks, integration CI, test-architecture validation, Backtest fast gates and release-readiness checks after their stable names are confirmed;
- require at least one independent approval;
- keep deployment/live authorization outside these branches.

This administrative limitation does not block preparation of the Draft integration PR.

## Known limitations

1. Exact executable canonical strategy composition is absent.
2. The optimized ingestion path has not been rerun over the full 12,968,018-row corpus.
3. Final-PR CI timing, queue and artifact aggregates must be recorded after the exact head completes.
4. Branch protection/rulesets could not be read or changed through the available connector.
5. Source benchmark environments are not identical across every workstream; cross-workstream performance numbers must not be compared as if produced by one runner.

## Provisional verdict

The integrated code tree is technically compatible across bounded Backtest/Core execution, registered streaming, runtime recovery, ingestion I/O, test architecture and CI policy. The conflict-sensitive merge has direct exact-tree evidence. Final Wave 1 disposition is conditional on exact-head Draft PR CI and remains strategy-specifically blocked by the missing executable canonical strategy composition.

## PR #306 stabilization validation

### Superseded failures

Head `a54a5fa22071053e10bf19a54c26ed27564e99c4` is historical evidence, not a validated base:

| Workflow | Run | Failure mechanism | Classification |
|---|---:|---|---|
| protection-target-ci | `30737534771` | 485 dependency tests discovered under a repository-local Python 3.13 environment | Test contamination / configuration |
| liquidity-sweep-hardening-ci | `30737534849` | approximately 498 tests discovered under `.venv` and dynamic uv cache trees | Test contamination / configuration |
| backtest-engine-fast | `30737534873` | `previous` inferred as `datetime` then assigned `datetime | None` at resume boundary | Static typing defect |

The Backtest run had already passed 268 tests per matrix, approximately 91% coverage, memory scalability, CLI and wheel build. Its failure was the final strict MyPy enforcement, not a runtime or recovery mismatch.

### Corrective scope

Test discovery now uses repository-aware tracked files plus untracked, non-ignored developer files and a tested generated-tree pruning policy. It ignores arbitrary virtual environments identified by `pyvenv.cfg`, dependency installation trees, uv/pip/build caches and untracked generated build/report/artifact trees. It does not ignore all hidden directories.

Contract tests prove that:

- a dynamically named environment containing `pyvenv.cfg` and dependency tests is ignored;
- a dynamically named uv cache is ignored;
- a real unmapped repository `test_*.py` remains visible;
- a real unmapped `conftest.py` remains visible;
- mapped repository tests and fixtures remain visible;
- parse errors and ambiguous suite mappings remain fail-closed;
- repeated inventory output is deterministic;
- tracked repository tests are not hidden merely because their parent directory resembles a generated output name.

Protection Target and Liquidity Sweep temporary environments and caches are located under `$RUNNER_TEMP`, creating a second containment layer outside the checkout.

Backtest declares `previous: datetime | None` for both registered and file resume paths. No timestamp validation, duplicate detection, gap detection, resume boundary, first/last candle validation, source identity validation, iterator closure, cursor semantics, traversal count or recovery digest operation was removed or reordered.

### Verified code candidate

Code candidate `4f03faba14f702a16fee7e3ff967877721e042a5` completed the exact-head stabilization matrix:

| Workflow | Run | Job | Result | Classification |
|---|---:|---|---|---|
| actionlint | `30740541900` | actionlint | PASS | Quality policy |
| runner-routing-policy | `30740541912` | policy audit | PASS | Quality policy |
| Fast checks | `30740541940` | fast-checks | PASS | Quality / typing / root tests |
| Test architecture validation | `30740541922` | Python 3.12 | PASS | Inventory and deterministic collection |
| protection-target-ci | `30740541797` | Python 3.12 | PASS | Component matrix |
| protection-target-ci | `30740541797` | Python 3.13 | PASS | Component plus monorepo compatibility |
| liquidity-sweep-hardening-ci | `30740541806` | Python 3.12 | PASS | Component matrix |
| liquidity-sweep-hardening-ci | `30740541806` | Python 3.13 | PASS | Component, repository compatibility and 1M restart stress |
| backtest-engine-fast | `30740541877` | Python 3.12 | PASS | Strict MyPy, 268 tests, coverage, memory, CLI, wheel |
| backtest-engine-fast | `30740541877` | Python 3.13 | PASS | Strict MyPy, 268 tests, coverage, memory, CLI, wheel |
| core-ci | `30740541916` | Python 3.12 | PASS | Core matrix |
| core-ci | `30740541916` | Python 3.13 | PASS | Core matrix |

A predecessor Backtest run on `c3850caf7a4b8f2d4440ca2c4877957b74ac2b4a` passed every substantive 3.13 gate but encountered `ECONNRESET` while uploading diagnostics. The rerun on the code candidate uploaded evidence successfully. Commit `4f03faba14f702a16fee7e3ff967877721e042a5` marks diagnostic artifact transport `continue-on-error` while preserving strict MyPy, tests, coverage, memory smoke and other final quality gates. Artifact transport failure is therefore reported as infrastructure evidence without converting a substantive code failure into success.

### Exact Test Architecture evidence

Artifact `8831150312` has digest:

`sha256:965aa8a095794f89e1e096f4a3e694600fe3c085f8ba6b4b022f6af4474f5a8d`

Its recorded facts are:

- 582 discovered test-named Python files;
- 579 mapped test files;
- 3 test-named non-test tooling files;
- 15 mapped fixture files;
- zero unmapped test files;
- zero unmapped fixture files;
- zero parse errors;
- 4,422 static test functions;
- 564 parametrized decorators;
- 22 suites and 6,995 collected IDs;
- exact repeated collection with zero added or removed IDs;
- audited base: 6,767 IDs;
- exact head: 6,995 IDs;
- 228 added IDs and zero removed IDs;
- optional-free root profile: 1,885 collected IDs on CPython 3.12.13.

### Semantic comparison

The stabilization patch is type/discovery/workflow containment work. Runtime trading and recovery semantics are supported by:

- unchanged validation and iterator-control structure in `streaming_data.py` except explicit nullable typing and formatting;
- passing strict MyPy on both Python matrices;
- passing full Backtest suite on both Python matrices;
- passing registered streaming and recovery tests included in the 268-test Backtest suite;
- passing memory scalability smoke and wheel/CLI checks on both matrices;
- exact prior direct/restarted digest evidence retained above.

No new full BTC workload was required because no production runtime algorithm changed in this stabilization patch.

### Stabilization limitations

- The exact executable canonical strategy composition remains absent, so a canonical-strategy legacy/streaming A/B is still not claimed.
- The complete 12,968,018-row ingestion corpus was not rerun for this stabilization-only work.
- Diagnostic artifact upload can be absent after a transient transport failure, but substantive quality gates remain independently enforced.
- Repository branch-protection/ruleset administration remains outside the available connector scope.

### Stabilization readiness

The code candidate is suitable for Wave 2 base review from the engine, Test Architecture, component CI and recovery-compatibility perspective. Strategy-specific production acceptance remains limited by the missing executable canonical strategy composition documented above.

The final documentation commit creates a newer pull-request head. PR #306 metadata and exact-head Actions on that final documentation SHA are authoritative for the ultimate coordinator verdict.
