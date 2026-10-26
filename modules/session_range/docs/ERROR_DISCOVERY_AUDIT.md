# Error Discovery Audit — Session Range 0.2.0

## Scope and synchronization

- Remote integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.
- Local candidate preserved before synchronization: `3b1372288dbb3d6c4be0467d6820fc7b7a62af22` plus a checksum-protected backup snapshot.
- Existing Draft PR #19 was not used as source of truth because its diff still contained interrupted transport/bootstrap material rather than the local 0.2.0 implementation.
- Real Core 0.4.0 wheel/API contracts and the integrated 0.1.0 Session Range wheel were used as the executable compatibility baseline.

## Error ledger

| ID | Severity | Symptom/root cause | Reproduction | Fix and regression status |
|---|---|---|---|---|
| ED-SR-001 | High | A restored completed feature could be re-emitted on a gap before gap invalidation. | Restore completed state, submit `data_gap=True`. | Gap handling now precedes re-emission; regression passes. |
| ED-SR-002 | High | An exact duplicate carrying `data_gap=True` returned before invalidating active state. | Active session + duplicate current candle + gap flag. | Gap invalidation runs before duplicate classification; both reason codes retained. |
| ED-SR-003 | High | An inferred timestamp gap removed local completed state but emitted no `FeatureDelete`, leaving Feature Store state stale. | Complete range, skip a candle without external gap flag. | Stream invalidation now emits deterministic deletes; regression passes. |
| ED-SR-004 | High | Extreme finite accumulated volume could overflow public float conversion after state mutation. | Two finite `1e308` volumes. | Numeric output is guarded and fails closed without active-state leak. |
| ED-SR-005 | Medium | Mapping configuration coerced identity values with `str`, accepting booleans/integers. | Boolean/integer name/timezone/start/end. | Strict string parsing and constructor validation. |
| ED-SR-006 | Medium | Nested manifest parameters remained mutable. | Mutate `parameters["sessions"][0]`. | Nested mappings are read-only proxies. |
| ED-SR-007 | High | Schema-v2 restore accepted malformed nested state, boolean counters and invalid OHLC geometry. | Corrupt active/cursor mappings. | Exact nested keys, strict scalar parsers and geometry invariants. |
| ED-SR-008 | Medium | Equivalent timezone offsets were retained rather than canonically serialized in UTC. | Restore `+02:00` timestamps equivalent to UTC bounds. | All restored timestamps normalize to UTC. |
| ED-SR-009 | High | Corrupt terminal dates and arbitrary schema-v2 completed feature IDs were accepted. | Invalid date suffix / changed `feature_id`. | Date parsing plus deterministic token/ID recomputation validation. |
| ED-SR-010 | Medium | Schema-v2 did not require a meaningful `module_version`. | Remove or replace with non-string value. | Nonempty string now mandatory for schema 2. |
| ED-SR-011 | High | Completed feature state could contain mismatched session/complete semantics. | Corrupt value or metadata fields. | Cross-field semantic validation added. |
| ED-SR-012 | Medium | Non-mapping or non-string-key top-level state could fail inconsistently or be silently ignored. | `restore_state([])` / integer key. | Explicit top-level contract validation. |
| ED-SR-013 | Medium | Direct `SessionRangeSpec` construction bypassed mapping-parser type checks, including timezone-aware clock values. | Construct dataclass with wrong runtime types. | Full dataclass runtime validation and naive-clock requirement. |
| ED-SR-014 | Low | Ruff formatter drift existed in benchmark/contract tests. | `ruff format --check`. | Files formatted; check passes. |
| ED-SR-015 | Medium | Strict cursor validation duplicated Core Candle validation on every event, causing measurable hot-path regression. | Interleaved baseline/candidate benchmark. | Trusted `Candle -> BarCursor` fast path; restore path remains strict. |
| ED-SR-016 | Medium | Benchmark memory tracing occurred before long workloads, making a monolithic run non-representative under throttled environments. | Full benchmark timed out while isolated workloads passed. | Memory measurement moved after throughput samples; extended/stress exposed as isolated profiles with regression tests. |
| ED-SR-017 | Low | Runtime `assert` statements protected completed-feature invariants, but disappear under `python -O`. | Bandit B101 plus AST scan of production source. | Replaced with explicit fail-closed exceptions; AST and invalid-feature regression tests pass in normal and optimized semantics. |

## Revalidation status

- 139 tests collected and passed.
- Statement coverage: 97% (`808` statements, `27` missed).
- Ruff lint and format: pass.
- strict mypy: pass against exact public Core import stubs.
- compileall: pass.
- wheel and source distribution: pass.
- clean wheel-only installation with Core 0.4.0 and PyYAML: pass; `pip check` clean.
- public import, entry point, processing smoke and checkpoint/restore smoke: pass.
- no critical/high defect remains open.

## Environment limitations

Python 3.12 is not installed in the current execution container. Bandit, detect-secrets and CycloneDX SBOM generation were executed locally. `pip-audit` was installed but its vulnerability query was blocked by DNS resolution of the external service, so dependency-vulnerability status remains a self-hosted CI gate. Repository-wide Python 3.12 validation remains pending self-hosted CI.
