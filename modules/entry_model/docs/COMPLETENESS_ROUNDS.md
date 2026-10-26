# Entry Model — Three Completeness Rounds

## Round 1 — functional completeness

Covered every supported entry kind for BUY and SELL, Decimal precision, exact and invalid geometry, missing prices, equality boundaries, cutoff/expiry, stale/future input, non-confirmed setup, unsupported schema, disabled entry kind, gap behavior, and rejection identity.

Findings fixed:

- identity collisions when execution-relevant fields differed;
- order-dependent behavior for conflicting revisions of one `setup_id`;
- malformed anonymous inputs sharing a rejection identifier;
- incomplete canonical identity for metadata/configuration;
- ambiguous legacy output parsing.

Result: the module deterministically transforms one canonical completed-bar setup into either one typed plan or one typed rejection without execution side effects.

## Round 2 — technical and architectural completeness

Validated the real Core 0.4.0 contracts:

- `StrategyModule`;
- `ModuleManifest`;
- `MarketView` and `ModuleDecision`;
- Feature Store visibility and lifetime;
- `ModuleRegistry` import path and entry-point loading;
- dependency planning;
- Core checkpoint capture/restore helpers;
- immutable output values;
- namespaced feature ownership.

Findings fixed:

- normalized Decimal configuration now has stable string representation and fingerprint;
- unknown fields fail closed;
- output schema 2 verifies canonical identity on read;
- state snapshot is JSON-compatible and bounded;
- no Core source change or parallel DTO system is required.

Result: the standalone package integrates through public Core APIs only.

## Round 3 — operational completeness

Exercised the lifecycle:

`constructor → validation → registry → engine start → same-bar feature processing → output/rejection → gap → checkpoint → restart → restore → continuation → finish`.

Adversarial coverage includes:

- exact and conflicting duplicate bars;
- exact and conflicting source features;
- conflicting setup revisions in either order;
- out-of-order bars before and after restore;
- stream symbol/timeframe changes;
- corrupted, partial, unknown-field, duplicate-row, oversized, wrong-schema, and wrong-config state;
- independent instances and independent streams;
- deterministic replay and hash-seed invariance.

Result: state recovery is atomic, configuration-bound, stream-bound, bounded, and deterministic.
