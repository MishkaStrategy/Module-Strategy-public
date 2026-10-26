# Changelog

## Unreleased

- Added `fvg_rt_frozen_1h_v1` and `fvg_rt_frozen_4h_v1` with exact Frozen component identity, stop-distance, TTL/max-hold/no-progress and priority provenance checks.
- Added the bounded `fvg_rt_final_15m_v1` profile with exact Final 15m identity, completed-timeframe, signal-key, geometry and execution-window validation; trusted Strategy Factory admission remains deferred until full FVG-RT composition parity.
- Added the `liquidity_sweep_v1` profile with strict event identity, causality, direction/level and source-bar geometry checks while leaving entry policy to Entry Model.
- Added a shared canonical setup schema-1 builder/validator for family-neutral adapter outputs.
- Added code-owned profile dispatch instead of a single hard-coded profile branch.
- Added the policy-free `canonical_setup_v1` source profile for already-canonical family setup features.
- Preserved `breakout_return_v1` default behavior while adding post-adapter fail-closed schema validation.

## 0.1.0 — 2026-07-22

- Added a strategy-independent canonical setup adapter for Module API 1.0.
- Added the `breakout_return_v1` mapping from `breakout_return.confirmed` to `setup.confirmed` schema 1.
- Added strict causality, symbol, timeframe, schema and directional price-geometry validation.
- Added deterministic canonical feature and rejection identities.
- Added bounded replay state with Core checkpoint/restore compatibility.
- Added real Core 0.4.0 integration tests covering Session Range → Breakout–Return → Adapter → Entry Model.
