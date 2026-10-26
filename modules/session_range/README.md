# Modular Session Range 0.2.0

`modular-session-range` is an external feature-producer package for **Modular Strategy Core 0.4.x** and Module API `1.0`. It builds deterministic OHLCV ranges for one or more configurable local-time sessions from completed base-timeframe candles.

## Responsibility

The module owns session-boundary resolution, incremental range accumulation, validation, diagnostics, and checkpoint/restore. It does **not** create entries, orders, stops, targets, position sizes, portfolio decisions, or exchange-specific behavior.

## Public API

```python
from modular_session_range import (
    ACTIVE_FEATURE,
    COMPLETED_FEATURE,
    MODULE_VERSION,
    STATE_SCHEMA_VERSION,
    SessionRangeModule,
    SessionRangeSpec,
)
```

Entry point:

```text
modular_strategy.modules / session_range
```

Produced features:

- `session_range.active`: point-in-time incremental range; `valid_until == created_at`.
- `session_range.completed`: final validated range available from the exact session close through the instant before the next enabled session starts.

Both feature values use schema version `1`. Module checkpoint schema is `2`; schema `1` checkpoints are migrated during restore.

## Configuration

```yaml
modules:
  - plugin: session_range
    enabled: true
    parameters:
      strict_data_validation: false
      sessions:
        - name: ny_first_4h
          timezone: America/New_York
          start: "00:00"
          end: "04:00"
          weekdays: [0, 1, 2, 3, 4, 5, 6]
          minimum_bars: 48
          minimum_range: "0"
          maximum_range: null
          emit_active: true
          dst_fold: 0
```

`weekdays` follows Python convention (`0` Monday, `6` Sunday). Sessions are half-open local intervals `[start, end)`. `end <= start` ends on the next local day; equal boundaries represent a 24-hour session. Nonexistent DST boundaries reject the session date. Ambiguous boundaries use `dst_fold=0` for the first occurrence or `dst_fold=1` for the second.

## Feature value

```text
session, session_date, timezone,
start_utc, end_utc, start_local, end_local,
open, high, low, close, midpoint,
width, width_decimal, width_pct,
volume, bar_count, complete
```

Financially meaningful aggregation uses a private 34-digit `Decimal` context with `ROUND_HALF_EVEN`; output float conveniences are emitted only when finite. `width_decimal` retains the exact internal decimal representation.

Completed feature IDs are deterministic and timeframe-aware:

```text
session-range-completed:{session}:{escaped_symbol}:{escaped_timeframe}:{session_date}
```

## Safety semantics

- Only `MarketView.current`, a completed candle, is processed.
- The first accepted candle must open exactly at the session start.
- Every later candle must open exactly at the preceding accepted candle close.
- A candle must be fully contained in the session.
- Completion requires the final candle to close exactly at session end.
- Missing-start, partial-boundary, gap, conflicting-duplicate, and ordering ambiguity fail closed.
- Exact duplicate candles are idempotent.
- Feature identifiers and stream keys escape delimiter characters.
- Runtime state is bounded: at most 64 session definitions, 1,024 tracked streams, 4,096 cached date bounds, and 65,536 scoped state entries per collection.
- Restore is atomic, schema-checked, configuration-fingerprint-checked, and cursor-consistency-checked.
- Restored completed features are re-emitted only when still valid and absent from the Feature Store view.
- Runtime invariants remain fail-closed under optimized Python; production source does not rely on `assert`.
- No system clock, filesystem, network, random UUID, or future bar is accessed.

## Verification

```bash
python -m compileall -q src tests benchmarks
ruff check src tests benchmarks
ruff format --check src tests benchmarks
MYPYPATH=typings python -m mypy --strict src/modular_session_range
coverage run --source=src/modular_session_range -m pytest tests
coverage report --fail-under=96
python -m build
python -m twine check dist/*
```

The full self-hosted workflow passed on uv-managed CPython 3.12 and 3.13 on macOS ARM64. It includes 139 tests, 97% coverage, repository/cross-module regression, clean installed-package smoke, security/SBOM checks and standard/extended/stress performance profiles.

Detailed evidence is in `docs/LOCAL_TEST_EVIDENCE.md`, `docs/BENCHMARK_REPORT.md`, `docs/REMOTE_CI_EVIDENCE.md` and `docs/FINAL_AUDIT.md`.

## Status

`VERIFIED RELEASE CANDIDATE — READY FOR MANUAL INTEGRATION REVIEW`

Draft PR #26 remains unmerged by project policy. All module-scoped self-hosted readiness gates passed.