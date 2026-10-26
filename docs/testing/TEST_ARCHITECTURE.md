# Test Architecture

## Audited state

This architecture was introduced from `integration/modular-strategy-full-rc` at
commit `30263f9e2bc4a972bc0027ffec40d23a0b3f2a37`.

The current static inventory records:

- 550 mapped Python test files;
- 15 mapped `conftest.py` files;
- 46 fixture definitions;
- 4,241 statically visible test functions;
- 553 parametrization decorators;
- 19 duplicate test-module basenames;
- zero unmapped test or fixture files;
- zero static parse errors.

The root `pyproject.toml` intentionally points `testpaths` at `tests`, so a bare
root `pytest` command is a Core/root-suite command. It is not evidence that
every separately packaged component suite was collected.

The authoritative all-project interface is the suite manifest and isolated
runner:

```bash
python scripts/test_suites.py list
python scripts/test_suites.py check all
python scripts/test_suites.py collect all --keep-going
python scripts/test_suites.py run all
```

Every owner suite is executed in a separate pytest process using its component
working directory. This preserves component-local `pyproject.toml` settings and
prevents repeated module basenames such as `test_config.py`, `test_state.py`,
and `test_integration.py` from sharing one import namespace.

## Validated dynamic evidence

The focused Python 3.12 validation workflow performs static inventory,
optional-dependency isolation, repeated isolated collection, and an audited-base
comparison in clean environments.

The validated evidence is:

- 22 isolated owner suites collected successfully;
- 6,775 exact node IDs on the head branch;
- two independent head collections matched exactly;
- audited base collected 6,767 exact node IDs;
- zero node IDs were removed relative to the audited base;
- the eight added IDs are the test-architecture contract tests;
- the full root owner suite collected 1,743 IDs;
- the optional-free root profile collected and passed 1,737 tests;
- `hypothesis`, `pyarrow`, and `matplotlib` were verified absent from the
  optional-free environment.

Collected IDs are discovery evidence, not a claim that all 6,775 tests were
executed by this workflow. Existing component and root execution workflows
remain authoritative for pass/fail coverage.

## Inventory

`config/test_suites.toml` is the machine-readable suite contract. It records:

- component ownership;
- test roots;
- suite-specific pytest arguments;
- required import dependencies;
- an exact installation hint;
- runtime class;
- expected CI frequency;
- logical test categories;
- controlled external boundaries.

`python scripts/test_inventory.py --fail-on-unmapped` builds a static JSON
inventory without importing project or optional packages. For each Python test
file it records the mapped owner suite, module basename, statically visible test
functions, fixtures, markers, parametrization decorators, imports, dependency
signals, async tests, and environment/network/filesystem/randomness/subprocess
signals. It also reports duplicate basenames and unmapped files.

Profiles are non-owning selections. They may select or deselect tests from an
owner suite, but they are deliberately excluded from static ownership mapping so
a file always has one authoritative owner.

Static analysis cannot truthfully provide expanded parametrized IDs, collection
errors, skipped, xfailed, deselected, runtime, or exact CI job ownership. Those
fields come from isolated pytest collection and execution. The inventory JSON
lists unavailable dynamic fields instead of inventing values.

A reproducible local baseline can be written outside the repository:

```bash
mkdir -p .local-tests/test-architecture
python scripts/test_inventory.py \
  --fail-on-unmapped \
  --output .local-tests/test-architecture/static-inventory.json
python scripts/test_collection_baseline.py collect all \
  --keep-going \
  --output .local-tests/test-architecture/collection.json
```

Do not commit large raw pytest logs or machine-specific paths.

## Suite taxonomy

The project uses these semantic categories:

| Category | Contract | Default frequency |
|---|---|---|
| `smoke` | Imports, CLI wiring, and minimum executable contracts | Every PR |
| `unit` | Isolated behavior without network or external services | Every PR |
| `integration` | Multiple components or controlled filesystem/storage boundaries | Every PR where practical |
| `regression` | Reproduction of a known defect or frozen behavior | Every PR |
| `property` | Hypothesis or equivalent invariant generation | Every PR or component gate |
| `performance` | Complexity, operation-count, memory-retention, or benchmark evidence | Focused/manual |
| `heavy` | Large datasets, long histories, high-volume recovery, or resource-intensive tests | Nightly/manual |
| `nightly` | Full dependency matrix, long regressions, security, and extended benchmarks | Scheduled/manual |

The manifest currently assigns categories at suite level. Existing component
markers remain authoritative inside their package. Per-test marker migration is
incremental: do not mass-mark tests or infer cost only from a filename.

## Marker policy

The target common marker vocabulary is:

```text
smoke
unit
integration
regression
property
performance
heavy
nightly
requires_pyarrow
requires_hypothesis
```

Existing root markers `slow`, `backtest`, and `checkpoint` remain valid and are
not redefined by this work. `slow` describes selection cost; it does not select
a runner. `backtest` is reserved for historical trading simulations or
backtest-derived research.

Global `--strict-markers` is not enabled in this migration. Several component
projects use independent pytest versions and configurations. Strict marker
validation may be enabled per owner suite after its unknown markers are proven
absent.

## Import mode and module collisions

The root owner suite explicitly uses:

```text
--import-mode=importlib
```

This is manifest-driven and also recorded in root pytest configuration. It
removes the four reproduced root collection collisions without changing
component-local import modes. Direct imports between test modules were audited
before enabling it, and exact node-ID comparison proved no root tests were lost.

There is no repository-wide command-line injection of importlib mode and no
`PYTHONPATH` or `sys.path` workaround. Component suites continue to use their
own working directory and pytest configuration.

## Optional dependencies

Missing mandatory dependencies are checked before any selected suite starts.
The runner returns exit code 2 and prints the suite-specific installation
command. It does not convert a missing mandatory suite dependency into a skip or
a green result.

| Dependency | Suites | Policy |
|---|---|---|
| `pyarrow` | Full root Backtest and Market Data | Required for the full listed suite |
| `hypothesis` | Full root and property-bearing component suites | Required when that full suite is selected |
| `matplotlib` | Signal Bot chart-rendering cases | Required only for chart-rendering coverage |
| `httpx` | Market Data application/control-plane tests | Required for the full Market Data suite |
| `pytest-asyncio` | Trading Runner | Required for async tests |
| SQLite | Results Aggregator, Research Runner, Strategy Factory | Standard-library local database only; isolated temporary paths required |

The non-owning `root-no-optional` profile excludes the two files dedicated to
`pyarrow` and Hypothesis and deselects the two parametrized matplotlib chart
reference cases. Root integration tests still require the base packages whose
entry points they exercise; the profile environment installs those packages
without their test extras.

```bash
python scripts/test_suites.py check root-no-optional
python scripts/test_suites.py collect root-no-optional
python scripts/test_suites.py run root-no-optional
```

The validation workflow creates a clean Python 3.12 environment, proves
`hypothesis`, `pyarrow`, and `matplotlib` cannot be imported, verifies that full
root preflight fails closed, then collects and executes the optional-free
profile.

A lightweight suite must not import a heavy optional dependency at collection
unless that suite explicitly requires it. A genuinely optional individual test
may use `pytest.importorskip`, but installation errors for required owner suites
must fail in preflight.

## Fixtures and test data

Fixtures must follow these rules:

- mutable state is function-scoped by default;
- session-scoped fixtures contain immutable or safely copied data only;
- filesystem writes use `tmp_path` or `tmp_path_factory`;
- environment changes use `monkeypatch` and are restored;
- background tasks, subprocesses, sockets, and event loops are closed in
  teardown even after failure;
- large datasets are replaced with the smallest evidence-preserving sample in
  ordinary suites;
- factories expose timestamps and important financial fields explicitly;
- timestamps are timezone-aware UTC values;
- factories may create invalid cases deliberately but must not hide required
  fields behind a universal “god factory”.

Shared factories should be introduced only where two or more suites use the
same public contract. Active Backtest/Core tests in the bounded-memory
workstream and active ingestion tests are not moved by this migration.

## Isolation and determinism

The runner starts each owner suite in a fresh process and supplies deterministic
defaults when the caller has not set them:

```text
TZ=UTC
PYTHONHASHSEED=0
```

The exact-ID recorder is launched without changing the suite working-directory
import semantics. It records node IDs through a pytest hook, so component
verbosity such as `-q` or `-qq` cannot silently erase parametrized IDs from the
evidence report.

Tests must remain independently executable as a single case, file, suite, and
repeated suite. They must not depend on order, repository-root writes, a user
home directory, fixed ports, shared SQLite files, module-global registries,
leftover caches, or wall-clock time.

Use fixed aware UTC timestamps, injected/fake clocks where existing production
boundaries permit them, explicit RNG seeds, and Hypothesis failure examples.
Do not disable Hypothesis shrinking or hide an ordering defect with reruns.

## Network and subprocess policy

Regular unit and integration suites must not require public network access.
Allowed boundaries are mocks, deterministic local servers, and explicit
contract fixtures. Live Binance, GitHub, or Google Drive checks require a
separate opt-in suite, timeout, rate-limit protection, and no real trading
credentials.

CLI/subprocess tests must use argument arrays rather than shell interpolation,
set an isolated working directory, bound execution time, capture stdout and
stderr, assert the exit code, and terminate child processes on failure.

## Performance tests

Correctness tests and benchmark evidence are separate. Regular PR checks should
prefer bounded-memory invariants, operation counts, collection limits, and wide
regression guards over tight wall-clock thresholds. Exact throughput and
machine-sensitive benchmarks belong to focused, nightly, or manual jobs with a
recorded input size, warm-up, repeat count, environment, metric, and baseline.

`research-runner-stress` is a distinct heavy owner suite and is not part of the
`components` group. It is included only in `all` and `heavy`.

## Commands

List and validate configuration without running tests:

```bash
python scripts/test_suites.py list
python scripts/test_suites.py check root
python scripts/test_suites.py check components
python scripts/test_suites.py check root-no-optional
python scripts/test_inventory.py --fail-on-unmapped >/tmp/test-inventory.json
```

Collect suites while continuing after independent collection failures:

```bash
python scripts/test_suites.py collect all --keep-going
python scripts/test_collection_baseline.py collect all \
  --keep-going \
  --output collection.json
```

Run project roots with fail-fast behavior:

```bash
python scripts/test_suites.py run root
python scripts/test_suites.py run components
python scripts/test_suites.py run root-no-optional
```

Pass arguments to pytest after `--`:

```bash
python scripts/test_suites.py run fvg -- -m "not slow" -x
python scripts/test_suites.py collect market-data -- -ra
```

The existing `scripts/run_local_test_suite.sh` remains the dedicated historical
Backtest dataset pretest. It is not replaced or repurposed.

## CI integration contract

The CI optimization workstream should consume the manifest rather than create a
second suite taxonomy. Required contracts:

1. Run `python scripts/test_inventory.py --fail-on-unmapped` as a fast gate.
2. Run `python scripts/test_suites.py collect <selected-group>` before execution.
3. Install every selected suite dependency before collection.
4. Preserve isolated component processes or equivalent isolated jobs.
5. Report collected, passed, failed, skipped, deselected, and xfailed counts.
6. Keep `root`, `components`, profiles, and heavy/nightly status visible
   separately.
7. Do not make full `all` execution a required PR check until runner capacity and
   measured runtime are established.
8. Do not weaken existing component coverage or quality thresholds.
9. Preserve exact node-ID evidence independently of pytest terminal verbosity.
10. Compare discovery against an audited base before discovery-affecting
    migrations.

The focused `Test architecture validation` workflow is intentionally limited to
architecture contracts and discovery evidence. It runs on `ubuntu-latest` so it
does not consume scarce self-hosted component capacity. Existing execution
workflows remain authoritative for production component behavior.

## Test-count comparison and lost-test proof

Before any rename, move, marker-based deselection, or discovery change:

```bash
python scripts/test_inventory.py --output before-static.json --fail-on-unmapped
python scripts/test_collection_baseline.py collect all \
  --keep-going \
  --output before-collection.json
```

After the change, repeat collection in the same dependency environment and
compare exact node IDs:

```bash
python scripts/test_collection_baseline.py compare \
  before-collection.json \
  after-collection.json \
  --output collection-diff.json
```

Use `--require-exact` when two runs of the same head must be identical. For an
audited-base-to-head migration, added IDs are allowed but every removed ID fails
the gate and must be explained as an intentional rename, consolidation, or
separately reviewed deletion.

The validation workflow checks out the audited base in a separate worktree,
installs it into a separate Python 3.12 virtual environment, collects it with
the same manifest and exact-ID recorder, and compares it with the head report.
This migration removed no test IDs.

The contract test `tests/test_test_architecture_contract.py` fails when a new
Python test file is not mapped by an owner suite, a configured path disappears,
the manifest references an unknown suite, a profile is accidentally treated as
an owner, the exact-ID recorder changes working-directory semantics, or static
parsing fails.

## Coverage

Coverage remains component-owned. Suite isolation must not change source roots,
branch coverage settings, path normalization, or thresholds. CI may combine
coverage only from compatible source configurations and must retain the
component reports needed to diagnose apparent increases or decreases caused by
path changes.

## Known limitations and migration guide

This migration deliberately does not:

- change production code;
- move or delete active Backtest/Core or ingestion tests;
- globally enable importlib mode for component suites;
- globally enable strict markers;
- reorganize existing component workflow ownership;
- execute every one of the 6,775 discovered tests in the focused architecture
  workflow;
- combine component coverage configurations.

Further work is evidence-driven:

1. Consume the manifest from the CI optimization stream instead of duplicating
   suite lists.
2. Add a Python 3.13 all-suite collection proof when capacity and dependency
   compatibility are ready.
3. Register unknown markers in their owning component before enabling strict
   marker validation there.
4. Continue targeted audits of the inventory’s filesystem, environment,
   randomness, subprocess, async, and network signals.
5. Split additional genuinely optional tests only when clean-environment evidence
   proves a useful lightweight profile.
6. Add smoke selections only after measured runtimes prove they are seconds,
   not merely named “smoke”.
7. Keep all discovery-affecting changes behind repeated exact-ID and audited-base
   comparisons.
