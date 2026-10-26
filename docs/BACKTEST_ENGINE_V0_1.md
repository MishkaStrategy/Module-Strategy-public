# Modular Backtest Engine v0.1

## Scope

`modular_backtest` is the application, data-ingestion and deterministic recovery
layer for the existing `modular_core` event-driven kernel. It does not duplicate
Module API, execution, portfolio accounting, feature storage, setup lifecycle,
audit or HTF aggregation implemented by Core.

```text
CSV / Parquet 5m Binance USDT-M data
        ↓
strict application configuration and validation
        ↓
modular_core deterministic event loop
        ↓
closed 15m / 1h / 4h / 1d events and module pipeline
        ↓
orders / fills / portfolio / audit / metrics
        ↓
optional checkpoint boundary → fresh prefix replay → exact state verification
        ↓
application + Core source provenance
        ↓
staging directory → SHA-256 artifact manifest → atomic publication
```

## Guarantees

- only `binance-usdt-m-perpetual` input is accepted;
- the base timeframe is fixed to 5m;
- symbols use uppercase alphanumeric `<BASE>USDT` syntax;
- timestamps must be UTC-aware and strictly increasing;
- duplicate timestamps are rejected;
- gaps are rejected or explicitly allowed by policy;
- timestamp and OHLCV logical fields must map to six distinct physical columns;
- boolean values are rejected as timestamps and OHLCV numbers;
- the source data file is hashed before and after parsing, and mutation during
  validation fails closed;
- file SHA-256 and validation metadata are exported;
- optional UTC range uses inclusive `start` and exclusive `end`;
- HTF specifications are UTC clock-aligned and gap-safe;
- execution remains conservative through Core defaults: next-bar eligibility,
  stop-first ambiguity handling, no exit on the entry bar and deferred same-bar
  stop-limit execution;
- unknown, missing and malformed YAML values fail with controlled configuration
  errors;
- module loading uses the controlled Core `ModuleRegistry`;
- funding is explicitly reported as disabled until a real funding model exists;
- application run identity and restart evidence do not depend on wall-clock time;
- the configured seed is applied before engine/module construction and replay, then
  the caller's Python `random` state is restored;
- every successfully started module receives exactly one `on_finish()` call,
  including strict exceptional paths;
- every completed result directory is checksummed and atomically published;
- every run records SHA-256 for the installed `modular_backtest` and `modular_core`
  source trees, application identity, package versions and the actual Git HEAD when
  available.

## Checkpoint and restart model

Core 0.4.0 exposes deterministic end-of-run module checkpoints, but does not expose
a public incremental kernel session that can safely deserialize broker, portfolio,
HTF, feature, setup and audit internals. v0.1 therefore implements a fail-closed
**deterministic prefix replay checkpoint**:

1. save the caller's Python `random` state and apply the configured seed;
2. process the continuous run to the configured checkpoint bar;
3. record the cursor, Core checkpoint, seed, actual source commit and SHA-256
   fingerprints of the complete runtime boundary, event stream and equity stream;
4. continue the original session to produce the continuous baseline;
5. reset the configured seed and create a fresh engine and fresh module instances;
6. replay the same validated prefix;
7. verify the complete runtime fingerprint before processing another bar;
8. continue and require the final result digest to equal the continuous digest;
9. call `on_finish()` exactly once for every successfully started module and restore
   the caller's original Python `random` state, including exceptional paths.

The runtime fingerprint includes portfolio and position state, all orders and broker
sequences, pending close, module states, aggregator buffers and counters, completed
HTF series, features, setup registry, audit records, event sequence and dispatch
cursors. A mismatch raises `ReplayRecoveryError`; continuation is forbidden.

This mode is deterministic crash recovery by replay, not direct broker snapshot
deserialization. It trades restart speed for a smaller and safer state contract.
Direct bounded snapshot restore remains a Core v0.2+ integration task.

Configuration:

```yaml
checkpoint:
  mode: replay            # disabled | replay
  restart_at_bar: 100000  # optional zero-based bar; midpoint when omitted
```

`restart_at_bar` must leave at least one bar for continuation. The output includes:

- `replay-checkpoint.json`;
- `restart-evidence.json`;
- continuous and restarted SHA-256 digests in `application-run.json`.

## Source provenance

`application-run.json` contains:

- application version;
- installed distribution version when available;
- SHA-256 of every `.py`, `.pyi` and `py.typed` file in `modular_backtest`;
- SHA-256 of the equivalent `modular_core` source tree;
- an application identity SHA-256 derived from both source hashes and versions;
- the actual `git rev-parse HEAD` when the package runs inside a Git checkout.

`MODULAR_STRATEGY_SOURCE_SHA` may explicitly provide a commit for an installed
artifact outside a checkout. It must contain exactly 40 hexadecimal characters.
`GITHUB_SHA` is deliberately not trusted because pull-request workflows may expose a
merge ref instead of the commit that was actually checked out.

The Core run id remains the compatibility identity generated by Core 0.4.0. Exact
application provenance is carried by application artifacts and their checksum
manifest. Reusing the same Core run id with different application source therefore
fails closed as an artifact conflict rather than overwriting the older result.

## CLI

```bash
python -m modular_backtest validate-data --config configs/backtest.example.yaml
python -m modular_backtest run --config configs/backtest.example.yaml
python -m modular_backtest inspect --result results/<run_id>
python -m modular_backtest compare results/<run_a> results/<run_b>
```

The `run` response includes `restart_verified`. It is `true` only after a configured
replay checkpoint has been reconstructed and the final result digest has matched.
`inspect` and `compare` verify the exact artifact set and every SHA-256 before reading
run metadata or metrics. Boolean values are never treated as numeric metric deltas.

## Result publication and artifacts

A run never writes directly into the final deterministic directory. It writes Core
and application artifacts into a sibling staging directory, writes
`artifact-manifest.json` last, verifies the staging manifest and atomically renames
the directory. A repeated run with an identical manifest reuses the existing result.
An incomplete, corrupted, symlinked or conflicting destination fails closed and is
never overwritten. Only the root manifest is excluded from its own checksum set; a
nested file with the same name is still hashed.

The result directory contains Core audit artifacts plus:

- `data-validation.json`;
- `application-run.json` with application/source provenance, seed, funding status,
  checkpoint mode and deterministic restart digests;
- `metrics-extended.json` with closed-equity and mark-to-market drawdowns, median
  trade, holding periods, commissions, slippage, side, monthly and yearly results;
- `restart-evidence.json` for every run;
- `replay-checkpoint.json` when replay mode is enabled;
- `artifact-manifest.json` with schema version, run id and SHA-256 for every other
  published file.

Extended `total_commissions` is the sum of fees from every execution fill, including
entry fills belonging to a position intentionally left open at the end of the test.

## Data formats

CSV is supported without optional dependencies. Parquet is processed in record
batches and requires the `backtest` extra:

```bash
pip install -e '.[backtest]'
```

Expected logical columns are open time, open, high, low, close and volume. Column
names are configurable; timestamp semantics are always bar open time. Invalid or
missing Parquet columns are wrapped as deterministic `DataValidationError` failures.

## Dependencies and trust boundary

- `PyYAML` (MIT) is required for safe YAML parsing; `yaml.safe_load` is used.
- `pyarrow` (Apache-2.0) is optional and used only for batched Parquet input.
- test, typing and security tools are development-only dependencies.
- module configuration is trusted repository configuration because importing a
  Python module executes code. Untrusted users must not control plugin paths.
- result writes are rooted under the configured output directory and use a
  deterministic, single-component Core run-id child directory.
- output roots, result destinations and published artifacts reject symlinks.
- replay checkpoint and artifact-manifest files are JSON data and never execute
  serialized code.

## Event ordering and no-look-ahead

The application passes only completed 5m candles to Core. The simulation clock
advances to bar close, earlier orders are processed, completed gap-safe HTF buckets
are published, immutable `MarketView` snapshots are built, modules run in dependency
order, decisions are applied and equity is recorded. A decision emitted for bar N
cannot fill on bar N. Replay uses the identical order and verifies the boundary
before continuation.

## Known v0.1 limitations

- one symbol per run and one net position per symbol;
- OHLCV simulation cannot infer the real intrabar path;
- no tick or order-book matching;
- no production funding-history loader;
- Parquet requires optional `pyarrow`;
- source integrity validation performs two full SHA-256 passes in addition to parse;
- Core materializes validated candles and retains the full audit/equity history;
- replay restart is O(prefix length) and requires the original validated input;
- checkpoint fingerprints depend on Core 0.4.0 private runtime structures, guarded
  by integration tests until Core exposes a public incremental session contract;
- Core run id does not directly embed the application source digest;
- direct snapshot restore, multi-symbol coordination and mark-price scenarios are
  deferred.

## Roadmap

1. public incremental Core session and versioned runtime-state DTO;
2. bounded direct snapshot restore with atomic validation and rollback;
3. incremental feed without full candle materialization;
4. funding-history loader with exact Binance timestamps;
5. signed data provenance sidecars;
6. application-aware Core run identity in the next compatible manifest version;
7. multi-symbol portfolio coordinator;
8. mark-price versus last-price scenario support.

## Heavy Audit

The heavy logic lives in the versioned, locally executable harness:

```bash
bash scripts/run_backtest_heavy_audit.sh <exact-commit-sha>
```

The reusable workflow supports both `workflow_dispatch` and `workflow_call`. A short
same-repository wrapper may invoke it from a Draft audit PR whose head starts with
`audit/backtest-engine-heavy-`; the wrapper passes the exact pull-request base SHA,
so the marker branch cannot substitute the audited source. Audit PRs are never
merged.

The Mac ARM64 harness:

- verifies the exact requested SHA;
- discovers an installed Python 3.12 or newer and creates an isolated venv;
- installs all development, Parquet and security dependencies;
- runs compile checks and validates its own shell syntax;
- runs Ruff, Ruff formatting and strict MyPy;
- runs the Backtest Engine suite with branch coverage and the full repository suite;
- repeats recovery tests under `PYTHONHASHSEED=1` and `9173` in independent processes;
- benchmarks 50k, 100k and 250k bars with replay restart and `tracemalloc`;
- builds and clean-installs a wheel;
- runs Bandit, detect-secrets, pip-audit and CycloneDX SBOM generation as independent
  fail-closed gates;
- writes runner, macOS, CPU, memory, Python, package, configuration and fixture
  provenance plus SHA-256 for every evidence file.

Fast CI runs `bash -n` against the harness on every harness/workflow change before a
Mac audit is accepted.
