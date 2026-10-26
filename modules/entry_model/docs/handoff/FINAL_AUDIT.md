# Entry Model 0.2.0 — Independent Final Local Audit

## Audit subject

Final standalone source, tests, package metadata, evidence files and release bundle. No remote service or remote CI result is used.

## Architecture and responsibility

PASS. The package imports only public Core contracts. It does not modify Core, place orders, size positions, calculate protection/targets, access an exchange, load market data, read system time or mutate another module. Feature keys are namespaced and dependencies are declared in `ModuleManifest`.

## Functional and mathematical correctness

PASS. Six entry kinds, BUY/SELL symmetry, Decimal precision, required price fields, directional geometry, equality boundaries, stop-limit offset and expiry/cutoff behavior are covered. NaN, infinity, zero and unsupported values fail closed.

## Temporal causality and no-look-ahead

PASS. Only the completed current bar and visible features are used. Future, stale and feature/setup causality violations are rejected. Truncated and extended engine runs prove no pre-confirmation output and future-data invariance.

## Public contracts

PASS. Import, entry point, manifest, required/produced features, DTO parsing, typed reason codes, JSON representation and legacy plan read compatibility are tested. Exact Decimal parameters are exposed as canonical strings.

## State, restart and ordering

PASS. State is versioned, bounded, JSON-compatible, config-bound and stream-bound. Restore is atomic. Corrupt, partial, oversized, duplicate, wrong-version and wrong-config snapshots are rejected. Exact duplicates are idempotent; conflicting duplicates and out-of-order bars fail closed before unsafe continuation.

## Determinism

PASS. SHA-256 identity is canonical, metadata key order and Python hash seed do not change plans, repeated workloads have identical digests, and restart continuation matches uninterrupted output.

## Performance and memory

PASS for module integration use. Throughput remains stable from 50,000 to 250,000 planning events, state remains capped, and there is no per-event history scan. Canonical serialization/hashing is the dominant measured cost and is required for auditability/determinism.

## Packaging

PASS. 316 tests, 99% coverage, Ruff, strict mypy, compileall, wheel/sdist build, pip check, clean installation, public import, entry-point discovery, real plan, checkpoint restore and real Core example all pass. The final ZIP was extracted, checksums verified, source recompiled, wheel rebuilt, tests rerun and rebuilt wheel installed in a new environment.

## Documentation and handoff

PASS. Baseline, completeness, 30 rounds, 15 audits, defect log, benchmark, local evidence, handoff, exact integration contract, file manifest and checksums are included.

## Accepted limitations

- Python 3.12 runtime was unavailable locally; Python 3.13.5 runtime and Python 3.12 static target were verified.
- One mutable instance is bound to one symbol/timeframe stream.
- State schema 1 requires explicit migration or clean start.
- Canonical `setup.confirmed` producer/adapter remains an integration dependency.
- Exchange/live chain is outside this module.

## Verdict

`LOCAL RELEASE CANDIDATE — READY FOR CODEX INTEGRATION`

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
