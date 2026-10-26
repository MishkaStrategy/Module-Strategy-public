# Market Regime state schema and migration

## Current schema

- Schema version: `2`.
- Serialization: JSON-compatible mapping.
- Decimal values: canonical strings.
- Timestamps: timezone-aware ISO-8601 normalized to UTC during restore.
- Collections: bounded lists restored into configured deques.

Fields:

- identity: `schema_version`, `config_signature`, `bound_symbol`, `bound_base_timeframe`;
- base cursor: `last_base_open_time`, `last_base_fingerprint`;
- target cursor: `last_open_time`, `last_fingerprint`;
- indicators: `processed_bars`, `prev_close`, `ema_fast`, `ema_slow`, `atr`, `tr_seed`, `closes`, `movements`, `slow_emas`, `atr_pct_window`;
- hysteresis: stable/candidate regime and volatility plus counts;
- health: data-gap resets, timeframe-gap resets, duplicate events and publications.

Derived sums are reconstructed from bounded collections and validated; they are not accepted from untrusted payloads.

## Atomic restore

A payload is parsed into a new `RuntimeState`. The live module state is replaced only after shape, types, capacities, enums, identity, config compatibility and internal consistency pass. Failed restore leaves the original state unchanged.

## Schema 1 migration

Schema 1 lacks `bound_base_timeframe`. Version 1.2 recovers it from the canonical saved base fingerprint.

Versions 1.0/1.1 included `publish_diagnostics` in the config signature. Version 1.2 treats diagnostics as operational, not semantic. It accepts both historical boolean signature variants when all semantic parameters match. Changed timeframe, periods or thresholds remain incompatible.

## Fingerprint validation

Saved fingerprint timestamp, symbol and canonical timeframe must match the explicit saved state. Numeric OHLCV components must be finite valid hexadecimal floats. Symbols containing `|` are supported through right-splitting the fixed numeric suffix.

## Compatibility

- Read schema 2: yes.
- Read schema 1: yes, with validated migration.
- Write schema 2: yes.
- Write schema 1: no.
- Unknown future schema: rejected.
