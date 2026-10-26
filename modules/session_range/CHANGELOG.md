# Changelog

## 0.2.0 — Local release candidate (2026-07-21)

### Added

- checkpoint schema 2 with canonical configuration fingerprint;
- bounded session, stream, cache, and restored-state limits;
- timeframe-aware delimiter-safe deterministic feature IDs;
- atomic restore validation and schema-1 migration;
- fixed private Decimal context and finite public-output guards;
- adversarial, property-style, deterministic replay, real-core integration, load, packaging, and clean-wheel tests;
- complete local Codex handoff and audit bundle.

### Fixed

- 24-hour session feature lifetime;
- Decimal NaN validation leaking `decimal.InvalidOperation`;
- ambient Decimal-context dependence;
- active-range/cursor mismatch acceptance on restore;
- legacy completed feature ID retained after schema migration;
- per-candle historical pruning hot-path scan;
- stale inactive stream cursors;
- source-tree test dependence on prior editable installation.

### Compatibility

Feature keys and payload schema remain unchanged. Schema-1 checkpoints are migrated. Exact completed feature IDs now include timeframe and should be treated as opaque.

## 0.1.0

Initial audited DST-aware, gap-safe Session Range feature producer.
