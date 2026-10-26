# State Schema 2 — Protection & Target 1.1.1

The snapshot is JSON-compatible and contains:

- `state_version`: integer `2`;
- `module_version`: `1.1.1` (compatible restore accepts `1.1.0` schema 2);
- `config_fingerprint`: SHA-256 of semantic configuration;
- `bound_symbol`, `bound_timeframe`: optional stream identity, set together;
- `last_bar_time`: UTC-aware ISO-8601 timestamp;
- `last_bar_signature`: eight canonical strings for timestamp, OHLCV, symbol and timeframe;
- `last_event_fingerprint`: SHA-256 binding the bar, gap flag, FeatureView timestamp and relevant
  feature fingerprints;
- `processed`: ordered, bounded rows containing process key and feature fingerprint;
- `counters`: non-negative integer observability counters;
- `state_digest`: SHA-256 of every other top-level field.

Restore is atomic: the complete candidate state is parsed and validated before any runtime field is
changed. Validation covers unknown/missing fields, version/config mismatch, stream identity, UTC
timestamps, OHLCV geometry, finite values, process-key length, duplicate rows, SHA-256 syntax,
counter fields, maximum state size and the top-level digest.

## Schema-1 compatibility

A checkpoint produced by module 1.0.0 is accepted only when:

- its module version is exactly `1.0.0`;
- its legacy configuration fingerprint matches the current semantic configuration;
- every schema-1 field passes strict validation.

It is then restored with no `last_event_fingerprint` and the next snapshot is emitted as schema 2.
The first repeated cursor after migration is handled fail-closed as a duplicate rather than
re-publishing a plan.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
