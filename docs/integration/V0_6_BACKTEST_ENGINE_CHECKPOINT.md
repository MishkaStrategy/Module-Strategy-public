# Modular Strategy v0.6 — Backtest Engine & Simulation checkpoint

Status: PUBLISHED AFTER POST-AUDIT CORRECTNESS REMEDIATION, FAST MATRIX AND EXACT HEAVY AUDIT

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Starting integration checkpoint: `7e09423f5f83c95f19d7b6702800db29df933cd1`
- Previously tested product head: `2198df1559d91a7ad2982a86bab4ddde152eb6fe`
- Pre-remediation live head: `acbf6fc12d7342e46c0cb1a069f44f0ed76428e9`
- Fast-green remediation head: `d7286d5acf7b16ff80fc205bf9da4147f70215d4`
- Canonical remediated component merge: `055ddc5720e3ffaeb7143c92a25d987b9dc72caf`
- Resolved integration source: `64b5c7d13268b870d501d37e5be3a008ba19bc2d`
- Ordinary two-parent central candidate merge: `ef340725a3b4edf63a6037a57a2be05984595a99`
- Core 0.4.2 / Backtest metadata reconciliation: `a98020c403ffb56706e1c08a339ceaa008176e91`

No merge targeted `main` or frozen Core.

## Original post-audit blocker

Five High correctness findings were recorded after the historical green Backtest audit. The previous live head contained only workflow-role changes and did not remediate them, so integration was blocked until new product evidence existed.

The remediation closes all five findings:

1. **Market identity mismatch** — Binance USDT-M perpetual data now accepts only Core `InstrumentSpec.market_type == linear_futures`; any explicit mismatch is rejected before engine construction.
2. **Contract multiplier attribution** — extended slippage cost now includes the validated `contract_size` used by Core execution.
3. **Finalization failure publication** — any journal error whose stage starts with `module_finish:` prevents staging and authoritative result publication.
4. **Non-finite JSON** — result serialization uses `allow_nan=False`; readers reject `NaN`, `Infinity` and `-Infinity`; comparisons treat only finite numbers as numeric.
5. **Durable publication** — every staging file and directory is synchronized before atomic rename, and the parent directory is synchronized after rename, with a Unix fallback where directory `fsync` is unsupported.

A dedicated five-test adversarial suite covers the corrected boundaries and publication order.

## Fast CI evidence

Permanent workflow `backtest-engine-fast`, run `30163783343`, exact remediation head `d7286d5acf7b16ff80fc205bf9da4147f70215d4`: **SUCCESS**.

Python 3.12 and 3.13 both passed:

- pinned uv/Python setup;
- dependency installation;
- compileall;
- heavy harness shell syntax;
- Ruff lint and format;
- strict MyPy;
- 118 Backtest tests;
- branch-aware coverage gate ≥90%;
- CLI smoke;
- wheel build.

Artifacts:

- Python 3.12: `sha256:c291cd8d44b6d3e53de6d8810ba6cccea0f2a508095af8e9ad121b39adaa86c8`;
- Python 3.13: `sha256:3141aaed4a2a55def2a141a7d298cad68b878d2c1a79d7bcab0c4625f0849284`.

The workflow was moved from runner-specific `actions/setup-python` toolcache behavior to pinned `setup-uv` 0.11.31 after repeated macOS toolcache installation failures occurred before code execution. All quality gates and thresholds were preserved.

## Exact Heavy Audit evidence

Permanent reusable Heavy Audit, run `30163900129`, exact canonical component SHA `055ddc5720e3ffaeb7143c92a25d987b9dc72caf`: **SUCCESS** on `[self-hosted, backtest]`.

All 17 gates passed:

1. exact commit verification;
2. isolated Python discovery;
3. environment installation;
4. compileall;
5. Ruff check;
6. Ruff format;
7. strict MyPy;
8. component tests;
9. full repository tests;
10. recovery seed 1;
11. recovery seed 9173;
12. deterministic benchmarks;
13. wheel and clean-install checks;
14. Bandit;
15. detect-secrets;
16. dependency audit;
17. CycloneDX SBOM.

Test evidence:

- Backtest suite: 118 passed, zero failures/errors/skips;
- full repository suite: 362 passed, zero failures/errors/skips;
- recovery seed 1: 10 passed;
- recovery seed 9173: 10 passed;
- Backtest branch-aware coverage: 93% over 1,082 statements and 338 branches.

Deterministic restart evidence:

- 50,000 bars, seeds 1 and 9173: identical continuous/restarted SHA-256;
- 100,000 bars: restart verified;
- 250,000 bars: restart verified;
- deterministic run IDs, configuration, data and module hashes retained across restart.

Performance evidence on the audited macOS ARM64 runner:

- 50k: approximately 794–819 events/s;
- 100k: approximately 801 events/s;
- 250k: approximately 824 events/s;
- 250k peak traced memory: approximately 899 MB;
- available runner memory: 16 GiB.

Heavy artifact `backtest-engine-heavy-30163900129`:

`sha256:2de9d3c5e259889e637b4063f41b37c736cef74978b48efb3822af5cb9afcbaa`

The artifact contains an empty `failures.txt`, exact `gates.tsv`, JUnit, coverage, benchmark, security, dependency, SBOM and checksum evidence.

## Integration metadata resolution

The Backtest branch carried a root Core 0.4.0-era `pyproject.toml`. Integration preserved current Core 0.4.2 ownership and added only:

- `modular-backtest` CLI entry point;
- optional `backtest` dependency group with `pyarrow>=16.0`;
- `modular_backtest` package data;
- checkpoint pytest marker;
- missing-import MyPy override for optional PyArrow.

Central Core version, description, security/dev dependencies, Breakout entry point and repository-wide Ruff/MyPy policy were retained.

## Behavioral boundary

- execution is deterministic and event-driven;
- UTC, 5m base data and strict market identity are enforced;
- no look-ahead is permitted;
- fees, slippage and contract multipliers are explicit;
- recovery compares continuous and restarted state hashes;
- result artifacts are canonical, finite, content-attested and durably published;
- component correctness does not establish strategy profitability.

## Safety state

- PR #168 remains open and Draft;
- no force-push, rebase or history rewrite;
- no merge to `main` or frozen Core;
- no tag, release, package publication or live execution.

## Next executable component

Results Aggregator & Experiment Registry remains blocked by three post-audit findings until a separate remediation is Fast/Heavy green:

- explicit SQLite single-thread ownership or a connection-per-thread model with independent deadlines;
- explicit audited POSIX-only backend capability contract;
- exact-type schema/version/revision decoding without permissive coercion.

A remediation branch may be prepared, but central Results integration is forbidden until those findings are closed with new evidence.
