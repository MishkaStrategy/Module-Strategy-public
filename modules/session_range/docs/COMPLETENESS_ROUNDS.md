# Three Completeness Rounds

## Round 1 — Functional completeness

Covered normal session accumulation, active feature emission, exact-boundary completion, minimum/maximum range filters, minimum bar count, weekdays, cross-midnight sessions, full-day sessions, DST summer/winter, DST ambiguity folds, nonexistent DST boundaries, missing session start, partial boundary candles, exact duplicates, conflicting duplicates, out-of-order candles, explicit and inferred gaps, strict and non-strict validation, invalid candle intervals, finite and non-finite numeric state, and multiple independent sessions/symbols/timeframes.

Defects fixed during this round:

- 24-hour feature validity failure;
- non-finite public float leakage;
- Decimal NaN validation order;
- timeframe omission in deterministic completed feature IDs.

Evidence: functional/edge/negative tests in `tests/test_release_candidate.py` and `tests/test_comprehensive_contract.py`, including future-data truncation equivalence and exact completion timing.

## Round 2 — Technical and architectural completeness

Verified the actual Core 0.4.0 public objects: `StrategyModule`, `ModuleManifest`, `MarketView`, `ModuleDecision`, `Feature`, `ModuleRegistry`, dependency planner, and Feature Store. Verified package entry point, public imports, immutable manifest/parameters behavior, namespaced features, no required upstream features, no concrete strategy logic in core, no core modification, JSON-compatible state, strict configuration parsing, schema versioning, deterministic identifiers, bounded caches, and safe downstream consumption.

Defects fixed during this round:

- delimiter-collision-prone stream/feature identities;
- unbounded session definition and stream counts;
- weak schema-v2 key/fingerprint validation;
- incomplete v1 feature-ID migration;
- inconsistent active range/cursor restore acceptance.

Evidence: registry/dependency planner integration test, same-bar Feature Store visibility test, entry-point clean-install smoke, and exact DTO/state contract tests.

## Round 3 — Operational lifecycle completeness

Verified:

```text
constructor -> validation -> registration -> on_start -> warm-up/no warm-up
-> processing -> feature/rejection -> gap/reset -> snapshot -> restart
-> restore -> conditional re-emission -> continuation -> on_finish
```

Adversarial restore cases include empty state, corrupted containers, unknown keys, incompatible schema, incompatible configuration fingerprint, unknown session, changed boundaries, duplicate active token, missing/mismatched cursor, invalid terminal scope, oversized collections, malformed stored feature, expired feature, and feature already present in Feature Store.

Operational isolation was verified for two module instances, parallel symbols, and parallel timeframes. Deterministic replay digests match. Checkpoint restore is atomic: a rejected state leaves the live instance unchanged.

## Result

All three completeness rounds are complete. No critical/high defect remains open. The module remains intentionally limited to session-range feature production.

GitHub CI: PENDING — clean branch validation has not completed.
