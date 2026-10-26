# Modular Strategy v0.6 executable-chain acceptance matrix

## Status and scope

This matrix covers the mandatory deterministic acceptance sequence for the executable chain staged on frozen Core 0.4.2, including the Trading Runner clean-room reconstruction in Draft PR #197.

The reconstructed Runner is not the lost implementation ending at `513726d89dca5a196b5f267c1462e41c1db7f4d2` and must never be represented as recovered source or identical history. Historical PR #93 remains blocked evidence only. The current candidate is an independently implemented, paper-only runtime over the accepted v0.6 contracts.

This is an integration checkpoint, not a release, merge, deployment or live-trading authorization. Signal Bot remains optional and outside this executable-chain gate.

## Acceptance scenarios

| # | Required scenario | Executable evidence | Expected invariant |
|---:|---|---|---|
| 1 | Setup confirmation only after the confirming bar closes | `modules/setup_adapter/tests/test_core_integration.py::test_real_pipeline_has_no_plan_before_return`; `::test_real_pipeline_emits_one_deterministic_entry_plan_same_bar`; `::test_future_data_does_not_change_emitted_plan` | No setup or entry appears before confirmation; later bars cannot alter the emitted plan. |
| 2 | Entry Model emits one deterministic plan | `modules/setup_adapter/tests/test_core_integration.py::test_real_pipeline_emits_one_deterministic_entry_plan_same_bar`; `tests/integration/test_v0_6_executable_acceptance.py::test_released_entry_protection_risk_chain_routes_and_executes_limit_order` | Exactly one released `EntryPlan` identity reaches Router normalization. |
| 3 | Protection emits valid side-specific geometry | `modules/trade_plan_router/tests/test_upstream_source_contract.py::test_released_protection_source_serialization_normalizes_without_fixture_drift`; v0.6 open-chain acceptance | Entry, stop and target remain linked and preserve side-specific geometry. |
| 4 | Risk approves or rejects without changing upstream evidence | `modules/trade_plan_router/tests/test_upstream_source_contract.py::test_released_risk_source_serialization_normalizes_without_fixture_drift`; v0.6 open-chain acceptance; Risk component suite | Approved quantity references the exact Entry and Protection IDs; rejection remains upstream-side-effect free. |
| 5 | Position Scaling partial approval preserves exact quantity | `modules/trade_plan_router/tests/test_upstream_source_contract.py::test_released_scaling_source_serialization_preserves_decrease_direction`; `tests/integration/test_v0_6_executable_acceptance.py::test_released_scaling_reduction_preserves_exact_quantity_and_reduce_only` | Quantity remains exact; reduction remains decrease-direction and reduce-only rather than becoming a close or add. |
| 6 | Router emits one canonical `DownstreamPlanEnvelope` | `modules/paper_execution/tests/test_router_cross_contract.py::test_canonical_open_envelope_executes_without_recalculating_router_semantics`; v0.6 open-chain acceptance | Canonical plan JSON, payload SHA-256 and OCC preconditions are unchanged downstream. |
| 7 | Paper Execution accepts an ordinary limit entry | `tests/integration/test_v0_6_executable_acceptance.py::test_released_entry_protection_risk_chain_routes_and_executes_limit_order`; Router/Paper cross-contract tests | One accepted plan creates one venue order and deferred protection without recomputing Router semantics. |
| 8 | Partial fill and duplicate fill event are idempotent | Paper coordinator and order-fingerprint suites | Cumulative fill advances once; exact duplicates are no-ops; conflicting duplicates fail closed. |
| 9 | Cancellation and expired plan are terminal and replay-safe | Paper Router cross-contract, coordinator and replay suites | Expired plans never reach OCC or venue; terminal cancellation cannot create a second side effect; restored replay is idempotent. |
| 10 | Duplicate plan is idempotent | Paper coordinator and Router cross-contract suites | Same plan returns the original result and never submits a second order. |
| 11 | Same plan ID with different content fails closed | Paper coordinator and replay-idempotency suites | Content mismatch wins over later barriers and produces no venue mutation. |
| 12 | Stop and target both inside one candle use conservative policy | `tests/integration/test_v0_6_intrabar_acceptance.py::test_same_bar_stop_and_target_uses_declared_conservative_stop_policy` | With `intrabar_priority="stop"`, the exit reason and price are the stop, never the target. |
| 13 | Synthetic OCO cancels only the safe sibling after factual confirmation | Paper OCO coordinator integration suite | Cancellation occurs only after a factual fill; duplicates do not repeat it; failed cancellation is never recorded as success. |
| 14 | Restart between entry and protection installation recovers safely | Paper protection-failure recovery suite | Pending protection and cancellation intent survive snapshot/restore and remain blocked until factual retry succeeds. |
| 15 | Lost acknowledgement and repeated event reconcile without duplicate venue effects | Paper replay, order-fingerprint and OCO suites | Replayed acceptance or repeated event reconstructs the same local result and cannot duplicate submission or sibling cancellation. |
| 16 | Stale mark, market mismatch, damaged checkpoint and state divergence block fail closed | Paper market-boundary, recovery-corruption and reconciliation suites | Invalid scope or state cannot submit; corruption cannot restore; divergence blocks; only uniquely owned, fully evidenced repair is permitted. |
| 17 | Runner startup restores and reconciles before readiness | `applications/trading_runner/tests/test_orchestration.py`; `test_runtime.py`; `test_paper_contracts.py` | Checkpoint restore and factual replay precede deterministic Paper scope reconciliation; a blocking report or checkpoint failure prevents readiness. |
| 18 | Runner market cycle survives every persisted crash stage without blind replay | `applications/trading_runner/tests/test_cycle.py`; `test_round5_audit.py` | Paper bar, factual routing, Core pipeline, prepared plan, dispatch, post-dispatch facts and cursor commit cannot be skipped or repeated outside their idempotency/reconciliation boundary. |
| 19 | Core journal facts are drained before and after dispatch with a durable cursor | `applications/trading_runner/tests/test_core_bridge.py`; `test_round5_audit.py::test_cycle_drains_factual_source_before_and_after_dispatch` | Fills caused by the bar and order events caused by dispatch both reach the canonical factual router; retry reoffers the identical batch until commit. |
| 20 | Duplicate completed market event preserves original plan identity | `applications/trading_runner/tests/test_cycle.py`; `test_journal.py` | Validator duplicate returns the recorded plan ID/hash or recorded no-plan result; conflicting or missing completion evidence blocks recovery. |
| 21 | Watchdog, kill switch, notification and shutdown remain decision-safe | `applications/trading_runner/tests/test_supervision.py`; `test_runtime.py` | Critical health activates the existing persisted kill switch; notification failure is isolated; bounded shutdown records forced fallback without creating trading decisions. |
| 22 | Runner package and public API remain paper-only | Trading Runner component gate and central executable acceptance | Python 3.12/3.13 strict typing, tests, sdist/wheel, clean import and static paper-only audit pass with no private credential or live execution surface. |

## Exact candidate gate

The exact candidate gate must run on Python 3.12 and 3.13 using `[self-hosted, fast]` and must include:

1. frozen Core contract and module-integration harness;
2. compile, Ruff and strict MyPy for the Trading Runner and integration acceptance surfaces;
3. the v0.6 cross-layer, intrabar and Position & Exit compatibility tests;
4. the complete Paper Execution component gate, including Router cross-contract, recovery, OCO, reconciliation, security, coverage, package reproducibility and clean-wheel smoke;
5. the complete Trading Runner suite, including real Core/Router/Paper contracts, crash recovery, Core bridge, supervision and paper-only boundaries;
6. package installation for Entry, Protection, Risk, Scaling, Router, Paper, Position & Exit and Trading Runner;
7. Trading Runner sdist/wheel build and `twine check`;
8. artifact upload with exact commit SHA, JUnit results, environment manifest and evidence/package SHA-256 files.

A green acceptance checkpoint does not authorize merge, release, production deployment or live execution. The Draft PR remains subject to explicit human authorization for any integration action.
