# Local Baseline Audit — Protection & Target

## Baseline identity

- baseline distribution: `modular-strategy-protection-target 1.0.0`;
- baseline wheel SHA-256: `68c4549832b5b0158556fa28f3f25459d987e6f1a7bfcee544c95b96a2ae5a8c`;
- source location: reconstructed byte-for-byte from the locally available verified wheel;
- Core: `modular-strategy-core 0.4.0`;
- Core wheel SHA-256: `62a283ffcf3014343bd12328b52abe11b1691b87a1d86bc3acac632750a13c9d`;
- Module API: `1.0`;
- Python available locally: `3.13.5`;
- Python 3.12 executable: not available in this sandbox;
- baseline state schema: `1`;
- public import: `modular_strategy_protection_target`;
- entry point: `protection_target` in `modular_strategy.modules`.

## Baseline contracts

- input feature: `entry_model.entry_plan` schema 1;
- output features: `protection_target.plan`, `protection_target.rejected`;
- invalidation modes: entry plan, fixed percent, structural feature;
- target modes: fixed R, structural target;
- bounded processed-plan state: 4,096 by default.

## Locally available pre-existing evidence

The source tests were not present in the wheel. A pre-existing local evidence archive recorded:

- 147 tests passed;
- 93% branch-aware combined coverage;
- Ruff, strict mypy, compileall, wheel and clean-install checks passed at the time the artifact was created;
- 2,000-plan benchmark median: 1.806872 seconds.

This evidence was treated only as baseline evidence. It was not described as a new GitHub CI run.

## Repeated local baseline checks

- fresh Python 3.13 runtime import: passed;
- entry-point discovery: passed;
- default protection plan smoke: passed;
- schema-1 snapshot/restore: passed;
- same-environment short benchmark, five runs:
  - median: 1.226193 seconds;
  - throughput: 1631.06 plans/s;
  - peak traced memory: 0.573992 MiB;
  - checkpoint: 235377 bytes;
  - restore median: 2.994 ms.

## Baseline findings

1. Decimal output used `normalize()`, which can depend on ambient Decimal precision.
2. Financial operations used the process-wide Decimal context.
3. Unsupported payload types fell back to `repr()`, potentially embedding process-specific addresses.
4. A stale FeatureView timestamp was accepted when it was not in the future.
5. Duplicate detection covered the bar but not changed feature/gap context.
6. Equally current structural features were selected by feature-id ordering rather than rejected as ambiguous.
7. Checkpoints lacked an integrity digest.
8. Diagnostic result lists could grow with an arbitrarily large same-bar batch.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
