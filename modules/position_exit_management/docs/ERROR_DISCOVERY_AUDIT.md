# Error Discovery Audit — Position & Exit Management 0.2.1

## Scope and provenance

- Repository: `mishkacher/Module-Strategy`
- Remote integration base: `integration/modular-strategy-v0.5.0`
- Base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Prior Draft PR: `#20`, head `33777705bfac9ce6990c3ef6b96f23d6782c996f`
- Preserved local RC SHA-256: `1a3244c7b027228d19025e97723f14231fa6c71664d1433001ea6e391269b296`
- Audit runtime: CPython 3.13.5
- Core contracts checked: 0.4.0 and 0.4.1
- Module API: 1.0
- Prior module version: 0.2.0
- Hardened module version: 0.2.1

The audit was executed before the new completeness and optimization rounds. The local release candidate was copied and checksummed before remote comparison. Direct network Git was unavailable in the execution container, so remote reads used the GitHub connector and all development occurred in a reconstructed local repository.

## Confirmed defects

| ID | Severity | Symptom and root cause | Reproducer | Fix | Recheck |
|---|---|---|---|---|---|
| ED-001 | High | A stop first observed after attachment was accepted as the initial stop, inventing an R denominator. `capture_initial_stop` allowed late reconstruction. | `test_late_valid_stop_does_not_reconstruct_unknown_initial_risk` | Initial risk is captured only in `PositionRuntimeState.from_position`; later stops update only observed protection. | PASS |
| ED-002 | High | The default no-op fast path hid `data_gap=True`, so no gap reason or status was emitted. | `test_default_fast_path_does_not_hide_data_gap` | Gap events bypass the fast path and execute the fail-closed gap branch. | PASS |
| ED-003 | High | A gap candle updated the favorable extreme before price actions were suspended, contaminating future trailing stops. | `test_gap_extreme_does_not_contaminate_future_trailing_state` | Gap rejection now precedes persistent extreme mutation. | PASS |
| ED-004 | High | Duplicate identity ignored position quantity/stop/identity, exit-pending state and flat-cleanup orders. Same-bar management changes were classified as exact duplicates. | Same-bar conflicting duplicate tests in `test_error_discovery.py` | Versioned SHA-256 identity includes every input consumed by the module; unconsumed active-position orders are intentionally excluded. | PASS |
| ED-005 | High | Semantically partial checkpoint states could restore: incomplete cursor pairs, active state without stream binding and negative retry indexes. | Restore corruption parameterizations | Added cross-field runtime invariants and atomic validation before state replacement. | PASS |
| ED-006 | High | Malformed PositionSnapshot/OrderSnapshot fields could mutate stream or cursor before an exception. | DTO mutation tests | `_validate_view` runs before preflight mutation and validates all consumed fields and duplicate order IDs. | PASS |
| ED-007 | High | A future-dated position could create active runtime state and favorable extrema before being rejected. | `test_future_position_is_rejected_without_creating_runtime_state` | Future entry rejection occurs before runtime creation. | PASS |
| ED-008 | Medium | High-precision Decimal configurations collapsed through float conversion and produced identical Core run-manifest module hashes. | `test_high_precision_decimal_configuration_has_distinct_core_identity` | Published module parameters use canonical Decimal strings; semantically equal values retain equal identity. | PASS |
| ED-009 | Medium | Expanded raw event identity could make checkpoint size proportional to pending-order history. | 1,000-order fingerprint test | Checkpoints store a fixed 64-character SHA-256 digest; legacy schema-2 fingerprints restore behind a fail-closed replay barrier. | PASS |
| ED-010 | High | An activated breakeven or trailing request was not retried after retracement when the adapter snapshot did not apply the prior update. | Breakeven/trailing retry tests | Activated components remain eligible; the module reissues the most protective legal stop behind the current market. | PASS |
| ED-011 | Critical | With `reset_on_scale_in=False`, scale-in during a pending partial could cause the retry to close the added quantity. | `test_pending_partial_does_not_close_scale_in_quantity_when_reset_is_disabled` | The pending target is increased by observed scale-in growth before residual reconciliation. | PASS |
| ED-012 | Medium | Combined BE/trailing output always labelled the action as trailing even when breakeven selected the tighter final price. | Combined component-selection test | The action/reason now identifies the component that selected the directionally tightest candidate; deterministic tie preference is trailing. | PASS |
| ED-013 | Medium | `Trade.position_closed=1` was truthy and could clear pending state despite not being a boolean. | `test_on_trade_rejects_non_boolean_closed_flag_before_mutation` | `on_trade` validates `position_id` and exact boolean type before mutation. | PASS |
| ED-014 | High | Corrupted partial state accepted duplicate/out-of-order completed stages, zero managed bars, impossible pending indexes and targets. | Checkpoint semantic tests | Completed stages must be a sorted contiguous prefix; pending stage must be next and target must be below last quantity; restored active state requires at least one managed bar. | PASS |
| ED-015 | High | Duplicate pending order IDs could emit duplicate cancel requests. | Duplicate order ID test | Duplicate IDs fail before cursor mutation. | PASS |
| ED-016 | Medium | Retry bookkeeping remained for an order that became unmanaged, consuming bounded capacity and affecting reused IDs. | Cleanup ownership transition test | Retry state is pruned against current managed candidates, not all visible orders. | PASS |
| ED-017 | Medium | State entry count was bounded but identifier length was not, allowing oversized checkpoint payloads. | Oversized identifier tests | IDs, setup IDs, stream strings and managed prefixes have explicit limits; tags have a separate processing bound. | PASS |
| ED-018 | Medium | The source distribution included tests but omitted `tests/conftest.py`, making unpacked sdist tests non-reproducible. | Built tar member inspection | Added `MANIFEST.in` and explicit reproducibility-file checks. | PASS |
| ED-019 | Low | Current Ruff formatter reported drift in ten source/test files. | `ruff format --check` | Reformatted without semantic changes and added formatter to the quality gate. | PASS |

## Environment findings that were not module defects

- Entry-point discovery failed before wheel installation; it passed after installing the built wheel.
- Initial strict mypy failed when Core was not installed in the environment; it passed in the correctly provisioned environment.
- Host `pip check` contains unrelated global packages; both clean module environments report no broken requirements.
- Local `pip-audit` could not query vulnerability services because DNS access to PyPI was unavailable. This remains a CI evidence item, not a passed local claim.
- CPython 3.12 was not installed locally; it must be verified by GitHub CI.

## Residual contract limitation

The Core snapshot does not expose original risk or original stop. A first attachment after the stop has already moved cannot reconstruct R without look-ahead or invention. The module safely disables R-based actions and emits `position_exit_management.missing_initial_risk`. The optional Core request is documented in `CORE_CHANGE_REQUEST.md`.

## Audit verdict

- Critical unresolved: 0
- High unresolved: 0
- Medium unresolved: 0
- Low unresolved module defects: 0
- External evidence gaps: Python 3.12 CI, dependency vulnerability service, self-hosted runner availability

The component is eligible to enter the new completeness and 30-round hardening cycle.
