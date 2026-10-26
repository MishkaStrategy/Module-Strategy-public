# Local Baseline Audit — Session Range

## Provenance

- Component: Session Range
- Source origin: locally reconstructed Session Range 0.1.0 wheel snapshot; the release candidate uses repository-relative paths only.
- Source artifact: `modular_session_range-0.1.0-py3-none-any.whl` from the supplied `integration-py312.zip` snapshot.
- Declared integration source: `integration/modular-strategy-v0.5.0`, commit `5e03b933207a99940244634f49a756d27888a5c3`.
- Local baseline Git commit: `0ca53530a93396cfd98647d4b17317a45bf749fb`.
- Baseline module version: `0.1.0`.
- Candidate module version: `0.2.0`.
- Core: `modular-strategy-core 0.4.0`.
- Module API: manifest `api_version="1.0"` (project documentation calls this Module API v1 draft revision 3).
- Local interpreter used: CPython `3.13.5`; package target remains Python `>=3.12` and Ruff/mypy target Python 3.12.

The original repository source tree was not present locally. The baseline Python source was restored verbatim from the wheel without inventing missing implementation. The wheel included package code and metadata but not the original test source. Baseline test evidence was supplied separately as JUnit XML.

## Baseline public contract

- Import: `modular_session_range.SessionRangeModule`.
- Entry point: group `modular_strategy.modules`, name `session_range`.
- Manifest name: `session_range`.
- Inputs: `MarketView.current`, `MarketView.data_gap`, and optional `MarketView.features` for restore re-emission reconciliation.
- Outputs: `ModuleDecision(features, skips, diagnostics)`.
- Produced feature keys: `session_range.active`, `session_range.completed`.
- Required features: none.
- Required timeframes: none; the module consumes the engine base timeframe.
- Feature payload schema: `1`.
- Baseline state schema: `1`.
- Baseline core dependency: exactly `modular-strategy-core==0.4.0`.

## Baseline evidence

- Supplied JUnit: 84 tests, 0 failures, 0 errors, 0 skipped, timestamp 2026-07-20T15:55:04Z.
- Baseline wheel imports and entry-point metadata were readable.
- Baseline performance was measured locally against the restored wheel on the same CPython 3.13.5 environment as the candidate.
- Baseline 10,000-event median: 0.239085 s (41,826 events/s).
- Baseline 50,000-event median: 1.167265 s (42,835 events/s).
- Baseline 10,000-event traced peak allocation: 12,465 bytes.
- Baseline checkpoint size: 1,028–1,042 bytes for measured workloads.
- Baseline restore latency: approximately 38–50 microseconds for measured workloads.

## Baseline limitations and defects reproduced

1. A 24-hour session could compute `valid_until < created_at`, causing `Feature` construction to raise `ValueError`.
2. Completed feature IDs omitted timeframe, allowing collisions across streams.
3. Restore did not require a configuration fingerprint and accepted more ambiguous state.
4. Restore validation was not fully atomic or bounded against adversarial collection sizes.
5. Decimal calculations could depend on ambient process decimal context.
6. Public float conversion did not consistently fail closed on values too large for finite floats.
7. Stream identity used unescaped delimiters.
8. Runtime stream count and session-definition count were not bounded tightly enough for hostile inputs.
9. Cache pruning scanned historical maps on every candle, creating an avoidable long-run hot-path cost.
10. `RangeAccumulator` could compare a NaN decimal before finite validation and leak `decimal.InvalidOperation` instead of a controlled contract error.

## Baseline quality-gate caveat

The baseline wheel did not contain the original tests or static-analysis configuration, so baseline Ruff, mypy, coverage, compileall, and clean source build could not all be reconstructed as source-tree commands. Candidate gates were therefore run against the restored source plus newly authored public-contract tests. Baseline test success is reported only from supplied JUnit evidence, not re-labelled as a locally executed baseline source suite.

GitHub CI: PENDING — clean branch validation has not completed.
