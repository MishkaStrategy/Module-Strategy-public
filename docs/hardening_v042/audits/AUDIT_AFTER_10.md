# Intermediate audit after rounds 09–10

## Decision

**Continue and publish checkpoint.** Run fingerprint canonicalization and parameter-schema preflight are deterministic, fail closed and compatible with every integrated module schema.

## Changes

- Signed numeric zero has one canonical config identity.
- Nested NaN/Infinity cannot enter run/config hashes.
- Parameter schema names, rule mappings, supported types, required flags, finite bounds, bound order, enums and defaults are validated at manifest construction.

## Public contracts and compatibility

No Module API field, import, callback or valid configuration changed. Existing audited schemas for Session Range, FVG, Market Regime, Liquidity Sweep and Breakout–Return pass unchanged. Invalid schemas now fail before registry/module side effects.

## Evidence

- Full repository suite: **368 passed**.
- Coverage: `TOTAL                                3354    394   1134    259    85%`.
- Ruff / strict mypy / compileall: pass.
- Test peak RSS: **68,948 KiB**.
- Event-loop median: **2.4115 s**, throughput **82,935 calls/s**.
- Multi-timeframe median: **2.2792 s**, peak RSS **100,208 KiB**, checkpoint **1169 bytes**.
- Both workload identities deterministic.

## No-look-ahead / checkpoint / replay

Configuration work occurs before execution and introduces no market visibility path. Recovery and replay suites remain green; run identities now reject non-standard numeric tokens.

## GitHub workflow status

- Remote v0.4.1 head is green.
- A v0.4.2 checkpoint push is authorized after this audit.
- Existing GitHub-hosted Linux CI will remain intact.
- A separate self-hosted macOS ARM64 checkpoint workflow will be added using the user-specified labels; actual runner availability must be confirmed by the resulting queued/run status because runner inventory is not exposed by the connector.

## Residual risks

Nested arbitrary object schemas are intentionally not recursively interpreted beyond their declared top-level type. Module-specific nested validation remains the module constructor's responsibility.
