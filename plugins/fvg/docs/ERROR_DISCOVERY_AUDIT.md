# Error Discovery Audit — FVG 0.2.0 → 0.2.1

Date: 2026-07-21.

## Scope and provenance

- Repository: `mishkacher/Module-Strategy`.
- Remote integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.
- Preserved local release candidate: FVG `0.2.0`, local commit `452a7e319b2eecf923e7666c9306c6c093c7a3f4`.
- Local release ZIP was copied before modification and its SHA-256 remained `e3f0aab4d2c3a6d27f8e00ccdd8c3243c40a2ba6b4113c800b8e266ad9e6b9d7`.
- Core under test: exact `modular-strategy-core==0.4.0` wheel.
- Module API: v1 draft revision 3.

The audit was performed before further optimization. Every confirmed defect first received a failing regression test.

## Findings

| ID | Severity | Symptom | Root cause | Reproducer / regression | Fix | Recheck |
|---|---|---|---|---|---|---|
| FVG-ED-001 | Critical | Replacing historical A/B candle content before C could silently change FVG output | Only the latest cursor candle was fingerprinted | `test_regression_historical_source_mutation_is_rejected_atomically` | Persist the last two source fingerprints; validate the exact source window before detection; keep validation atomic | PASS |
| FVG-ED-002 | High | A configured timeframe omitted on a gap could later recreate a zone from pre-gap history | Gap barrier was created only for timeframes present in the gap view | `test_regression_gap_with_missing_timeframe_blocks_old_history_recreation` | Create a fail-closed cursor/barrier for every configured timeframe on gap | PASS |
| FVG-ED-003 | High | Same timestamp with changed `open` or `volume` was accepted as exact duplicate | Fingerprint/signature covered only high/low/close | `test_regression_open_or_volume_change_is_a_conflicting_duplicate` | Include full OHLCV event payload in duplicate identity | PASS |
| FVG-ED-004 | Medium | Semantically equal Decimal configuration strings generated different checkpoint fingerprints | Fingerprint used non-canonical Decimal text | `test_semantically_equal_decimal_parameters_have_same_fingerprint` | Canonical Decimal serialization; preserve exact v2 fingerprint function for migration | PASS |
| FVG-ED-005 | Medium | Installed FVG wheel was not PEP 561 typed | Missing `py.typed` package marker and package-data declaration | `test_distribution_declares_pep561_marker` plus wheel content inspection | Add `py.typed` and setuptools package-data | PASS |
| FVG-ED-006 | Medium | New integrity checks initially reduced throughput by about 45% | Repeated SHA-256/Decimal work on retained source bars | Five-run benchmark and cProfile | Bounded in-memory references for immutable Core Candle; cryptographic fallback for replacement/custom CandleLike; precomputed close-time fingerprint path | PASS; final change versus 0.2.0 RC is within 5% |
| FVG-ED-007 | Medium | Schema 2 checkpoints could not carry the new retained-history proof | State layout had no source-history fingerprints | `test_v2_checkpoint_migrates_to_v3_and_preserves_active_zones` | State schema 3; strict schema 1 and 2 migration; post-migration causal barrier | PASS |

## Repository and package audit

- No parallel FVG implementation was found in the local RC.
- No Core source was changed.
- Public import remains `modular_strategy_fvg:FvgModule`.
- Entry point remains `modular_strategy.modules:fvg`.
- Produced feature keys remain unchanged.
- Feature schema remains `1`.
- Module version is now `0.2.1`; state schema is `3`.
- State schemas `1` and `2` remain readable with fail-closed migration.

## Verification after fixes

- 207 pytest cases passed.
- Branch-aware coverage: 96%, gate 95%.
- Ruff: PASS.
- strict mypy: PASS, five source files.
- compileall: PASS.
- wheel and sdist: PASS.
- wheel contains `modular_strategy_fvg/py.typed`: PASS.
- clean wheel-only installation with exact Core 0.4.0: PASS.
- `pip check`: PASS.
- public import, entry point and checkpoint smoke: PASS.
- deterministic replay and no-look-ahead regression suites: PASS.

## Security/tooling availability

`bandit`, `detect-secrets`, `pip-audit` and CycloneDX tooling were not installed in the offline local environment. They were not falsely reported as executed. The self-hosted CI workflow installs and runs them at the final GitHub checkpoint. Local source was additionally checked for committed secret-like values and unintended broad suppression patterns.

## Verdict

All confirmed critical/high defects were fixed before post-error optimization. No Core Change Request is required.

GitHub CI for this exact commit: pending publication and self-hosted execution.
