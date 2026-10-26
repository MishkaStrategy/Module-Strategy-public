# Changelog

## 0.3.0 — 2026-07-23 — release validation candidate

Status: **not released; exact-head release validation in progress**.

- Activated the split causal runtime and public package version 0.3.0.
- Added state schema 4 with atomic restore and strict schema-1/schema-2/schema-3 migration.
- Added deterministic zone lifecycle state, fill/penetration tracking, expiry and archival events.
- Added confirmed evaluation metrics and configurable gap, price and volume filters.
- Added causal pre-FVG evaluation, persistent provisional candidates and deterministic resolution events.
- Added typed timeframe overrides and bounded expanded configuration.
- Hardened custom candle-adapter UTC, historical fingerprint and evaluator boundaries.
- Preserved bounded state, single-symbol scope, gap/order barriers and deterministic IDs.
- Added Python 3.12/3.13 exact-head release validation, reproducible wheel/sdist checks, clean installs, security audits and CycloneDX SBOM evidence.
- Updated regression suites and documentation to the 0.3.0/schema-4 public contract.

No merge, tag, package publication or release is authorized until the complete exact-head release matrix passes.

## 0.2.1 — 2026-07-21 — error-discovery hardening

- Added two-source historical fingerprints and rejected changed A/B candles atomically.
- Fixed data-gap barriers when a configured timeframe is absent from the gap view.
- Extended conflicting duplicate identity to full OHLCV payload.
- Canonicalized Decimal configuration fingerprints while preserving schema-2 migration.
- Added state schema 3 with strict schema-1/schema-2 migration and corrupted-history validation.
- Added PEP 561 `py.typed` packaging.
- Expanded the suite to 237 tests and 96% branch-aware coverage.
- Optimized immutable Core Candle history validation to keep the five-run hot-path change versus 0.2.0 within the 5% gate.
- Added Error Discovery Audit, post-fix baseline, self-hosted CI and refreshed release evidence.

## 0.2.0 — 2026-07-21 — local release candidate

- Added strict boolean, timeframe and bounded-integer validation.
- Added single-symbol instance binding.
- Added conflicting-duplicate detection with persisted bar fingerprints.
- Added explicit gap/order causal barriers and fixed cross-gap three-candle reuse.
- Added bounded active-zone state and atomic maximum checkpoint size enforcement.
- Added state schema v2 with strict corrupted-state checks and v1 migration.
- Added canonical timezone-normalized IDs while preserving all former UTC IDs.
- Added canonical zone-ID verification on restore.
- Added immutable public zone payload mappings and finite-float output protection.
- Added typed reason/error classes and richer structured diagnostics.
- Preserved feature schema v1 and Core v0.4.x contracts.
- Expanded local suite to 179 cases with 95% branch-aware coverage.
- Profiled and optimized hot path; final regression versus v0.1.0 baseline is 2.38%, within the 5% gate.
- Added local wheel/sdist, clean-install, benchmark, audit and Codex handoff evidence.

GitHub CI was intentionally deferred for the 0.2.0 candidate.

## 0.1.0 — 2026-07-20

- Original audited strict three-candle bullish/bearish FVG producer.
- Lifecycle features, gap/order protection, and versioned checkpoint/restore.
- Recovered for this local session from the exact integration artifact at source commit `5e03b933207a99940244634f49a756d27888a5c3`.
