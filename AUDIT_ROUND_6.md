# Audit after optimization round 6

## Scope

Checkpoint compatibility, run identity, strict module configuration, public API exports, Python packaging and CI readiness after rounds 5–6.

## Validation performed

- Full pytest suite: **81 passed**.
- Statement coverage: **88%** (required minimum: 85%).
- Source and tests compile successfully.
- Wheel `modular_strategy_core-0.4.0-py3-none-any.whl` built successfully.
- Run IDs verified to change when module parameters change and remain stable when inputs are unchanged.
- Checkpoints round-trip through atomic JSON storage and restore module state with version/schema checks.

## Findings fixed

1. **Module parameters absent from run identity — critical reproducibility defect.** Two parameterizations of the same module previously produced the same `module_hash` and `run_id`. Resolved parameters are now embedded in `RunManifest` and its hash.
2. **No standard module restart contract — high operational risk.** Added `snapshot_state` / `restore_state`, versioned module-state records and a terminal kernel checkpoint on every run.
3. **Checkpoint compatibility ambiguity — high operational risk.** Restore now verifies module name, module version and state schema version.
4. **Ambiguous module YAML — medium configuration risk.** Unknown keys, dual class/plugin selection, non-boolean enable flags and non-mapping parameters are rejected before import/instantiation.
5. **No automated compatibility gate — medium maintenance risk.** Added GitHub Actions for Python 3.12/3.13, compilation, tests, coverage threshold and wheel build.
6. **Version drift — low release risk.** Package, kernel and public module versions are aligned at `0.4.0`.

## Remaining limitations

- A checkpoint captures module state and run identity, but the batch backtest engine does not yet resume mid-file with reconstructed broker/timeframe buffers.
- JSONL structural verification is not a cryptographic hash chain.
- Backtest execution remains OHLC-based and therefore cannot resolve unknown intrabar paths beyond configured conservative policies.
- The core remains one net position per engine instance; portfolio-level multi-symbol orchestration belongs above this kernel.

## Decision

Round 6 is accepted. The remaining limitations are explicit architectural boundaries, not hidden defects.
