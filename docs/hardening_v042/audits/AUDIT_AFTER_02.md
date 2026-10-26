# Intermediate audit after rounds 01–02

## Decision

**Continue.** Strict numeric and configuration typing changes reject only invalid runtime coercions. The complete repository suite, component static gates, deterministic benchmarks and recovery tests are clean.

## Changes

- Shared finite/positive/non-negative validators reject Python `bool` rather than silently converting it to `0` or `1`.
- Invalid non-numeric values receive a stable `TypeError` at the public validation boundary.
- Engine and execution boolean switches enforce actual `bool` values.
- Data interval and bounded-history fields enforce positive non-boolean integers.

## Invariants and public contracts

- Valid numeric values and valid YAML/Python configurations preserve previous behavior.
- Dataclass fields, constructor ordering, defaults, public import paths, event ordering, checkpoint schema and Module API are unchanged.
- Only values inconsistent with the declared public types fail earlier.
- No strategy-specific branch was introduced into the kernel.

## Local evidence

- Full repository suite: **321 passed**.
- Combined branch-aware coverage: **84%**.
- Statements: 3,237; missed: 388.
- Branches: 1,036; partial branches: 246.
- Component Ruff: pass.
- strict mypy: pass across 25 core source files.
- compileall: pass.
- Test peak RSS: **73,024 KiB**.
- Test elapsed: **1.67 s**.

## Performance

### Event loop

- Workload: 20,000 bars × 10 modules = 200,000 module calls.
- Five runs; median: **2.1060 s**.
- Throughput: **94,965 calls/s**.
- Run identity deterministic: yes.

### Multi-timeframe load

- Workload: 30,000 bars × 5 modules with 5m/15m/1h histories.
- Three runs; median: **2.3996 s**.
- Throughput: **62,511 calls/s**.
- Peak RSS: **97,560 KiB**.
- Checkpoint size: **1,169 bytes**.
- Run identity deterministic: yes.

## Determinism, no-look-ahead and recovery

- Numeric/config preflight occurs before processing and does not alter bar visibility.
- No event timing, feature timestamp or completed-bar semantics changed.
- Checkpoint/restore and replay regression suites pass in the full repository run.
- Existing deterministic run IDs repeated exactly across benchmark runs.

## Compatibility

- Core 0.4.1 callers providing valid types remain compatible with candidate 0.4.2.
- Integrated module configurations use valid booleans and integers and remain accepted.
- No migration is required.

## GitHub workflow status

- Last published remote head `362e11b5876fdab82bbc1f5183fc9630dcdc6187`: all recorded GitHub workflows passed.
- Candidate 0.4.2 is not pushed yet; GitHub CI for these two rounds is therefore pending.
- Self-hosted runner labels cannot be enumerated through the current connector and remain to be verified before workflow migration.

## Residual risks

- Runtime type validation is not yet equally strict across every string, enum and optional configuration field; subsequent rounds address these boundaries.
- Repository-wide formatting/lint debt in unrelated module packages remains outside this core PR.
