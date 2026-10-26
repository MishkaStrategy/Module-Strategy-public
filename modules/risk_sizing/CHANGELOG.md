# Changelog

## 1.2.0 — 2026-07-21

- added immutable `PositionAdjustment` and `PositionAdjustmentDecision` schema-1 contracts;
- preserved ordinary `risk.request` schema-1 behavior without breaking changes;
- added transition parsing for Position Scaling 0.2.2 metadata geometry;
- independently recalculated projected quantity, weighted average entry, notional, leverage and total risk;
- applied instrument `contract_size` consistently to existing-position geometry;
- made approvals represent the permitted delta quantity, including partial approvals;
- prevented existing-position increases from consuming another `max_positions` slot;
- added fail-closed evidence, portfolio-snapshot and duplicate/conflict validation;
- added Python 3.12/3.13 self-hosted packaging, security and SBOM gates.

## 1.1.0 — 2026-07-21

- aligned the module with `integration/modular-strategy-v0.5.0`;
- added strict feature freshness, schema and symbol checks;
- preserved `setup_id` in typed decisions;
- added typed request/decision mapping round-trips;
- hardened Decimal, configuration and nested DTO validation;
- added stream binding and idempotent malformed-event handling;
- upgraded bounded checkpoint schema to version 2 and fixed real-core tuple restore;
- added standalone wheel, entry point and PEP 561 marker;
- added 120 tests, 96% branch-aware coverage, profiling and restart/load evidence;
- optimized the pure calculator beyond the recorded 1.0.0 baseline.

## 1.0.0

Initial fixed-fractional risk sizing module.
