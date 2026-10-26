# Optimization Wave 1 Integration Ledger

## Status

- Integration branch: `integration/optimization-wave-1`
- Target branch: `integration/modular-strategy-full-rc`
- Verified target head at integration start and immediately before publication: `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`
- Code candidate head before adding this ledger and the validation documents: `383e69657777f8bcd8918b1b1fc35740f93f9234`
- Integration policy: ordinary history-preserving merge commits; no squash, rebase, force-push, source-PR merge, release, deployment or live execution.
- Final disposition: integrated candidate remains Draft and targets only `integration/modular-strategy-full-rc`.

The final pull-request head necessarily differs from the code candidate head because this ledger, the validation report and the Wave 2 backlog are committed afterwards. The pull-request metadata is the authoritative final head identity.

## Live source inventory

| Source | Branch | Verified head | Disposition |
|---|---|---:|---|
| PR #294 | `integration/registered-streaming-on-backtest-memory` | `bd498ac1e71c8a68cb26a98b56bce7ab5999bf8f` | Integrated as the canonical Backtest/Core/registered-streaming source. |
| PR #290 | `optimization/historical-data-ingestion-io` | `40d306174dea4a8bba6805a79220df2a93d83ba8` | Integrated. |
| PR #289 | `optimization/test-architecture` | `3eb5c9b9c4c6f098dfe4771a0a9f61e8faec67ca` | Integrated. |
| PR #291 | `optimization/github-actions-ci` | `b81fd1fcce53d288bb7b0a286782668f1d99ea34` | Integrated after production and test changes. |
| PR #288 | `audit/project-wide-optimization-review` | `bc2b786311966292637a7c1f80a68c846115ae94` | Integrated last as documentation and backlog evidence. |
| PR #287 | `perf/backtest-memory-scalability` | `6a60dc0607dd255c4a29dc55bc4c97e96c2c0a5f` | Two post-#294 commits were discovered live and integrated as a late delta after conflict validation. |
| PR #292 | `integration/registered-streaming-on-backtest-memory` ancestry | `e89f68b5dd0dae90a36a83637ad9a8d27eef867a` | Not applied separately; its canonical changes are carried by PR #294. |
| PR #297 | validation-only | `b75715b37521902d33d392eb1a219478cef1de22` | Evidence only; not integrated. |
| PR #298 | research baseline | `7a0cf8eea6ea2b67873ac976fb9e14c542e84ed1` | Evidence only; not integrated. |

All source PRs remained open and Draft during integration. No source PR was merged automatically.

## Integration order and rollback points

| Order | Source | Transport PR | Merge / integration commit | Rollback point before step |
|---:|---|---:|---:|---:|
| 1 | PR #294 | #299 | `f34dc7c2876f53bd3e9adf7cf04be931bd139405` | `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37` |
| 2 | PR #290 | #300 | `e4930548b68fdae35b8370a5ba443fff3dbcf693` | `f34dc7c2876f53bd3e9adf7cf04be931bd139405` |
| 3 | PR #289 | #301 | `dff83028fb2989098ffde41077f53bf372efb90b` | `e4930548b68fdae35b8370a5ba443fff3dbcf693` |
| 4 | PR #291 | #302 | `5844a42d722fe6da32e868b01b3528426e388c73` | `dff83028fb2989098ffde41077f53bf372efb90b` |
| 5 | PR #288 | #303 | `7b9cd9382bdc8d8d608d8007c792e02b44c18ac6` | CI reconciliation commits following `5844a42d...` |
| 6 | late delta from PR #287 | #304, validated in Draft lab PR #305 | `580f12cb3ea7da699cc30b1c97abc485a835f322` | `7b9cd9382bdc8d8d608d8007c792e02b44c18ac6` |
| 7 | late Backtest MyPy gate | connector commit | `383e69657777f8bcd8918b1b1fc35740f93f9234` | `580f12cb3ea7da699cc30b1c97abc485a835f322` |

The #287 late-delta merge has two parents: the Wave 1 integration tree and `6a60dc0607dd255c4a29dc55bc4c97e96c2c0a5f`. It was fast-forwarded into the integration branch; no ref was force-updated.

## Conflict graph and decisions

### `scripts/audit_runner_routing.py`

PR #290 and PR #291 both changed runner routing policy. The final file preserves:

- repository-wide allowed selectors and forbidden physical labels;
- ingestion-specific benchmark routing;
- the `market-data-ingestion-io-benchmark.yml` backtester allowlist entry;
- reusable-workflow handling;
- the prohibition on undocumented `max-parallel: 1`.

Resolution commits:

- `89c3a1c4c0c8e43eba51f21a99c803f0b53329e1` — pre-resolution before the CI merge;
- `43a4f5871794da55a6d231d0630cbcd5576588ae` — final union after the merge.

Rejected alternatives: selecting either branch wholesale or dropping the ingestion workflow from the backtester allowlist.

### `.github/workflows/backtest-engine-fast.yml`

The final workflow preserves the PR #294 bounded-memory smoke and quality gates while applying PR #291 supply-chain policy:

- immutable checkout and upload-artifact SHAs;
- read-only checkout credentials;
- artifact retention;
- Python 3.12/3.13 matrix;
- compileall, shell syntax, Ruff, Ruff format, strict MyPy, pytest, coverage, CLI, wheel and memory smoke;
- the late #287 `import-untyped` MyPy exception scoped only to this strict gate.

Resolution commits:

- `f95c0ee4254d1e503deafb12bc05f1308d2ec4fc` — integrated hardened workflow;
- `383e69657777f8bcd8918b1b1fc35740f93f9234` — late MyPy delta applied through the GitHub connector because the isolated Actions token did not have workflow-write scope.

Rejected alternatives: restoring mutable Action tags, removing the memory smoke, or weakening the final gate to diagnostic-only success.

### Late PR #287 delta

Live ancestry showed that current PR #287 had two commits not contained in PR #294. The actual three-way conflict set was exactly:

- `src/modular_backtest/runner.py`;
- `src/modular_backtest/scalability/engine.py`;
- `src/modular_backtest/streaming_data.py`.

Resolution:

- use the late #287 runtime-session engine refactor;
- preserve PR #294 registered streaming source mode, cursor recovery and batch-size contract;
- route registered streaming replay through cursor recovery;
- retain legacy materialized deterministic replay for registered rollback mode;
- use runtime snapshots for ordinary non-registered checkpoint audit;
- add a common recovery metadata adapter for disabled, deterministic replay, registered cursor replay and runtime snapshot results;
- add exact tests for recovery mode, simulation passes, source traversals and rows replayed.

The isolated merge laboratory verified the conflict list before applying any resolution. Run `30735370347` passed compileall, Ruff check, Ruff format and 100 focused tests. Its validated merge result was commit `580f12cb3ea7da699cc30b1c97abc485a835f322`.

Rejected alternatives:

- omitting the two late #287 commits;
- applying all of PR #287 separately before checking ancestry;
- replacing the registered streaming path with local-file-only runtime recovery;
- assuming every restarted result has the same recovery mode;
- force-pushing a handcrafted tree.

## CI and supply-chain reconciliation

Post-merge policy commits:

- `20d6e663977eacf5bf81d408a739f7f89d7f92d2` — ingestion multi-symbol workflow hardening;
- `3792e34ae8b6a73bf2a3988311671b034fa82814` — ingestion I/O benchmark workflow hardening;
- `cd56f104ad40fa1067c106b41a007d2860c67414` — test architecture workflow hardening;
- `8e077aabf93b629d91607264f217b11a4ad4e41b` — zero mutable Action reference baseline.

The action-pinning baseline now contains no grandfathered mutable references. Every external Action reference is required to be a 40-character immutable commit SHA.

## Changed component groups

- `src/modular_core` and `src/modular_backtest`: bounded histories, bounded journal/event tail, resource guards, runtime snapshot recovery, result digest and registered streaming recovery compatibility.
- `modules/market_data_ingestion`: spool/storage I/O optimization, streaming dataset reader, benchmark harnesses and tests.
- Test architecture: `config/test_suites.toml`, isolated subprocess runners, collection/inventory tooling, optional-free profile and workflow.
- GitHub Actions: immutable pinning, setup-uv manifest, routing, concurrency, retention, caching and policy tests.
- Audit documentation: project-wide matrix, baseline and prioritized backlog.

## Hygiene and exclusions

Not integrated:

- PR #297 benchmark harness/evidence branch;
- PR #298 research strategy implementation;
- raw market data or registry bytes;
- local filesystem paths;
- credentials or exchange secrets;
- releases, tags, deployment or live-trading changes.

The canonical strategy name `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short` was searched in repository content. No exact executable YAML/module composition was found. This is recorded as an acceptance limitation, not replaced with the negative PR #298 research baseline.

## Administrative state

The available GitHub connector did not expose repository ruleset or branch-protection administration endpoints. No rules were changed. Required-check recommendations are recorded in the validation report and Wave 2 backlog.

## Final disposition

The Wave 1 tree integrates the required PRs in the prescribed order, incorporates the verified post-#294 PR #287 delta, keeps PR #292 from being double-applied, and leaves evidence-only PRs outside production integration. The final Draft PR remains the authority for exact-head CI status and the final integration SHA.

## PR #306 stabilization evidence

The original production head `a54a5fa22071053e10bf19a54c26ed27564e99c4` remains retained as superseded evidence. Its exact-head failures were:

- `protection-target-ci` run `30737534771`: Python 3.13 Test Architecture inventory included 485 dependency tests from a repository-local virtual environment;
- `liquidity-sweep-hardening-ci` run `30737534849`: Python 3.13 inventory included approximately 498 files from `.venv` and dynamic uv cache trees;
- `backtest-engine-fast` run `30737534873`: both matrices reached final enforcement after 268 passing tests, but strict MyPy reported a nullable resume timestamp assignment in `streaming_data.py`.

The stabilization descendant applies only narrow corrective changes:

- repository-aware discovery of tracked files plus untracked, non-ignored developer files;
- fail-closed generated-tree pruning for arbitrary `pyvenv.cfg` environments, `site-packages`, `dist-packages`, uv/pip/build caches and untracked build/report/artifact trees;
- explicit contracts proving real unmapped tests, real unmapped `conftest.py`, parse errors and ambiguous mappings remain visible;
- temporary Protection Target and Liquidity Sweep environments moved to `$RUNNER_TEMP`;
- `previous: datetime | None` made explicit on both registered and file resume paths;
- Backtest diagnostic artifact transport separated from substantive quality enforcement after a confirmed transient `ECONNRESET` upload failure.

No trading rule, fill, stop, target, fee, funding, slippage, data schema, public API, recovery algorithm or source traversal behavior was intentionally changed.

Code candidate `4f03faba14f702a16fee7e3ff967877721e042a5` passed the complete stabilization fence:

| Workflow | Run | Matrix / job | Result |
|---|---:|---|---|
| actionlint | `30740541900` | actionlint | PASS |
| runner-routing-policy | `30740541912` | policy | PASS |
| Fast checks | `30740541940` | fast-checks | PASS |
| Test architecture validation | `30740541922` | Python 3.12 | PASS |
| protection-target-ci | `30740541797` | Python 3.12, 3.13 | PASS / PASS |
| liquidity-sweep-hardening-ci | `30740541806` | Python 3.12, 3.13 | PASS / PASS |
| backtest-engine-fast | `30740541877` | Python 3.12, 3.13 | PASS / PASS |
| core-ci | `30740541916` | Python 3.12, 3.13 | PASS / PASS |

Test Architecture artifact `8831150312`, digest `sha256:965aa8a095794f89e1e096f4a3e694600fe3c085f8ba6b4b022f6af4474f5a8d`, records:

- 579 mapped test files;
- 15 mapped fixture files;
- zero unmapped test files;
- zero unmapped fixture files;
- zero parse errors;
- 22 suites collected twice with exact repeatability;
- 6,995 exact-head IDs versus 6,767 audited-base IDs;
- 228 added IDs and zero removed IDs.

This section records the verified code candidate. The pull-request head after documentation commits is authoritative for the final SHA and its CI state. PR #306 must remain Draft and must not be merged as part of stabilization.
