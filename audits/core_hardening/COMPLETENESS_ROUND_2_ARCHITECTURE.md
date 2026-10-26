# Completeness round 2 — architecture and integration contracts

## Scope

Module manifests, dependency planning boundaries, feature contracts, event identity and timeframe semantics.

## Confirmed defects and fixes

- Zero-length timeframes such as `0m` were parsed as valid: rejected.
- Manifest names could be whitespace-only: rejected.
- Duplicate required timeframes/features and required/optional overlap were ambiguous: rejected before planning.
- Invalid API-version text and invalid warm-up declarations were accepted too late: rejected at manifest creation.
- Numeric parameter schemas accepted NaN: fail-closed validation added.
- Feature namespaces with blank segments and blank producers were accepted: rejected.
- Feature snapshots accepted naive query timestamps: UTC-aware boundary enforced.
- In-memory event store allowed mixed run IDs: rejected.
- Event envelopes allowed non-positive sequence numbers and self-causation: rejected.

## Compatibility

All changes reject previously invalid/ambiguous input. Valid manifests and the five integrated analytical modules retain the same public contract.

## Verdict

Architecture remains plugin-oriented and strategy-agnostic; dependency semantics were tightened without API redesign.
