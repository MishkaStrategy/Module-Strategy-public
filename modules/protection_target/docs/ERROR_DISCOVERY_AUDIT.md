# Error Discovery Audit — Protection & Target 1.1.1

## Scope and source comparison

- remote repository: `mishkacher/Module-Strategy`;
- remote integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`;
- remote module branch before hardening: `modules/protection-target` at `789680102439fc5b1eed34e2d950cd06be9e0fa8`;
- existing Draft PR: #12;
- remote module version before synchronization: 1.0.0;
- preserved local release candidate before audit: 1.1.0 at local commit `63e41a4366ec52efc2fbd518ab25692b8511c83b`;
- local backup ZIP SHA-256: `544daa23cfaaf3778b62483f418a79724c5207a1b96d772f8e67c912fcf0f393`;
- audit interpreter: CPython 3.13.5;
- Core: 0.4.0 / Module API 1.0.

The local candidate was newer than the remote branch and was backed up as both ZIP and Git bundle before any synchronization work.

## Findings

| ID | Severity | Symptom | Root cause | Fix and regression | Status |
|---|---|---|---|---|---|
| PT-ERR-001 | High under optimized Python | Seven production invariants disappeared under `python -O` | Runtime `assert` was used for configuration, FeatureView and state assumptions | Replaced with explicit exceptions; AST scan and optimized subprocess regression | Fixed |
| PT-ERR-002 | High contract integrity | Boolean `entry_plan_id`/`setup_id` could become strings and produce a plan | IDs were normalized with unconditional `str()` | IDs now use strict `safe_identifier`; boolean regressions | Fixed |
| PT-ERR-003 | High Module API compatibility | Extreme Decimal parameters became `inf` or `0.0` in `module.parameters` | Unchecked Decimal-to-float conversion | Finite/non-underflow manifest conversion and boundary tests | Fixed |
| PT-ERR-004 | High replay integrity | Same-bar duplicate after schema-1 migration was silently treated as verified | Legacy state has no event fingerprint | Duplicate at legacy cursor now fails closed; next completed bar continues | Fixed |
| PT-ERR-005 | Medium state contract | `True`, `2.0` and `"2"` were accepted as state schema 2 | Python equality was used instead of exact type validation | Exact non-boolean integer validation | Fixed |
| PT-ERR-006 | Medium state robustness | Non-JSON checkpoint values leaked raw `TypeError` | Digest serialization errors were not normalized | Wrapped as `ProtectionTargetStateError`; strict JSON digest | Fixed |
| PT-ERR-007 | Medium bounded-state guarantee | Several checkpoint/config strings had no explicit maximum | Validation checked syntax but not allocation bounds | Added explicit limits for keys, paths, identities, timestamps and numeric text | Fixed |
| PT-ERR-008 | High restart compatibility | A patch release would reject compatible schema-2 state from 1.1.0 | Module version matching was exact | 1.1.1 accepts 1.1.0 schema-2 state and re-emits 1.1.1 | Fixed |
| PT-ERR-009 | Medium public API determinism | Direct `ProtectionTargetConfig` construction with raw strings silently bypassed Enum branches | Dataclass annotations were not runtime-enforced | Explicit Enum-type validation and tests | Fixed |
| PT-ERR-010 | Medium mutation safety | `module.parameters` and nested manifest schemas were externally mutable | Plain dictionaries escaped through public API | Added mapping proxies at both levels | Fixed |
| PT-ERR-011 | Medium payload contract | Payload schema accepted numeric `1` through string coercion | `str(schema_version)` comparison | Requires exact string `"1"` | Fixed |
| PT-ERR-012 | Medium setup matching | Boolean structural setup ID could match string setup `"True"` | Structural selector used `str(candidate)` | Selector uses strict safe identifiers | Fixed |
| PT-ERR-013 | Medium deterministic failure policy | Unsupported feature values raised a low-level contract error before module classification | Fingerprinting occurred outside normalized stream-error handling | Feature fingerprint failures become `ProtectionTargetDataError` | Fixed |
| PT-ERR-014 | Medium checkpoint parser | Mixed/non-string top-level keys and invalid module-version types could leak raw exceptions | Key sorting/set membership assumed strings/hashable values | Strict top-level key and bounded module-version validation | Fixed |
| PT-ERR-015 | Medium bounded observability | Counters had theoretically unbounded integer serialization | Python integers are unbounded | Signed 64-bit ceiling in increment and restore | Fixed |

Every finding has a reproducible regression in `tests/test_optimized_runtime.py`, `tests/test_post_error_hardening.py`, or the existing contract/state suites.

## Repository and packaging checks

- no duplicate runtime implementation found in the local package;
- public import and entry point match the remote 1.0.0 contract;
- PEP 561 marker is present;
- no Core source was copied or changed;
- wheel and source distribution build locally;
- clean wheel installation and Registry loading pass;
- Bandit: zero findings after PT-ERR-001;
- detect-secrets: zero findings;
- SBOM generation: passed;
- dependency vulnerability audit: **not completed locally** because the sandbox could not reach the public vulnerability index. This is a CI follow-up, not a green result.

## GitHub and runner discovery

The repository contains a reusable self-hosted workflow using actual labels:

```yaml
runs-on: [self-hosted, macOS, ARM64]
```

The old module workflow still used a GitHub-hosted Ubuntu matrix. The module hardening workflow must preserve Linux compatibility while making heavy jobs manual/self-hosted and adding concurrency, timeouts, cleanup and evidence retention.
