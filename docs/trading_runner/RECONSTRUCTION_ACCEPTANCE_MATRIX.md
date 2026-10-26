# Trading Runner clean-room reconstruction acceptance matrix

## Identity and safety

- Base checkpoint: `integration/modular-strategy-v0.6.0-rc` at `c2df6430234ed13d2cbdc08cf109274a1e737c1d`.
- Reconstruction branch: `runtime/trading-runner-reconstruction-v0.6`.
- Canonical Draft PR: `#197`.
- Verified exact candidate head: `8c4aae125ac2a8bd0d2e2f5c07cd252779918f6b`.
- Historical coordination PR #93 remains blocked and is not the source of this implementation.
- This implementation is a clean-room reconstruction and is not byte- or history-identical to lost head `513726d89dca5a196b5f267c1462e41c1db7f4d2`.
- Execution mode is strictly `paper`; live/private trading paths are forbidden.
- Merge, release, deployment and live execution remain unauthorized.

## Five-round result

| Round | Scope | Result |
|---:|---|---|
| 1 | Configuration, lifecycle, bootstrap, recovery/reconciliation gate, plan idempotency, kill switch foundation | `PASS` |
| 2 | Core Module Registry/Pipeline integration, market-event validation and bounded ordering/backpressure | `PASS` — Intermediate Audit 1 |
| 3 | Execution/position factual routing, durable journal/checkpoint/recovery, crash consistency | `PASS` |
| 4 | Watchdog, failure isolation, observability, graceful/forced shutdown, security and resource limits | `PASS` — Intermediate Audit 2 |
| 5 | Durable application composition, Core bridge, central v0.6 acceptance, packaging and exact-candidate gate | `PASS` |
| Final Audit | Architecture, state ownership, causality, recovery, operational safety and evidence | `PASS` |

## Mandatory acceptance scenarios

| # | Scenario | Result | Evidence |
|---:|---|---|---|
| 1 | Invalid or non-paper configuration fails before dependency initialization | PASS | `test_config.py`, `test_runtime.py` |
| 2 | Dependency ordering is sourced from Core `ModuleRegistry.validate_and_plan` | PASS | `test_pipeline.py`, `test_real_contracts.py` |
| 3 | Recovery completes before reconciliation; reconciliation completes before market-data start and readiness | PASS | `test_orchestration.py`, `test_runtime.py`, `test_paper_contracts.py` |
| 4 | Market events cannot enter the module pipeline outside an admissible runtime state | PASS | `test_service.py`, `test_runtime.py` |
| 5 | Stale, future, duplicate, out-of-order, malformed and gap-bearing market events follow explicit fail-closed policy | PASS | `test_market_data.py`, `test_cycle.py` |
| 6 | Router `DownstreamPlanEnvelope` is forwarded without changing action, quantity, protection, content hash or OCC evidence | PASS | `test_paper_contracts.py`, `test_real_contracts.py` |
| 7 | Duplicate plan ID plus identical content returns the first result without a second Paper side effect | PASS | `test_runtime.py`, `test_cycle.py` |
| 8 | Duplicate plan ID plus changed content fails before Paper Execution | PASS | `test_runtime.py`, `test_cycle.py` |
| 9 | Kill switch blocks risk-increasing plans while allowing risk-reducing actions | PASS | `test_runtime.py` |
| 10 | Failed pending-entry cancellation remains persisted and prevents kill-switch reset | PASS | `test_runtime.py`, checkpoint tests |
| 11 | Restart never auto-resets an active kill switch | PASS | `test_runtime.py`, checkpoint tests |
| 12 | Paper Execution or reconciliation ambiguity blocks new risk without disabling factual recovery | PASS | `test_orchestration.py`, `test_round5_audit.py` |
| 13 | Notification failure is isolated from execution and state mutation | PASS | `test_supervision.py` |
| 14 | Critical queue overflow fails closed; critical events are never silently dropped | PASS | `test_market_data.py`, `test_routing.py` |
| 15 | Checkpoint and journal corruption prevent readiness and require recovery | PASS | `test_checkpoint.py`, `test_journal.py` |
| 16 | Graceful shutdown checkpoints and stops in deterministic order; timeout uses explicit forced fallback | PASS | `test_runtime.py`, `test_supervision.py` |
| 17 | Deterministic replay preserves lifecycle order, event/plan identities and final state | PASS | `test_checkpoint.py`, `test_routing.py`, `test_cycle.py` |
| 18 | Paper bar mutation cannot be blindly replayed after an ambiguous crash | PASS | `test_cycle.py`, `test_round5_audit.py` |
| 19 | Prepared plan identity is durable before dispatch and drift blocks before a second side effect | PASS | `test_cycle.py`, `test_round5_audit.py` |
| 20 | Core journal facts are drained before and after dispatch with an exact durable cursor | PASS | `test_core_bridge.py`, `test_round5_audit.py` |
| 21 | Duplicate completed market event preserves its original plan ID/hash or no-plan result | PASS | `test_cycle.py`, `test_journal.py` |
| 22 | Python 3.12 and 3.13 component gates pass on `[self-hosted, fast]` | PASS | run `30194713646` |
| 23 | Clean wheel installation and public imports pass with no live-execution dependency or credential surface | PASS | run `30194713646` |
| 24 | Full v0.6 executable chain passes with Core, Setup/Entry, Router, Paper, Position & Exit and Trading Runner installed together | PASS | run `30194713641` |

## Exact verification evidence

### Component gate

- run: `30194713646`;
- candidate: `8c4aae125ac2a8bd0d2e2f5c07cd252779918f6b`;
- Python 3.12 and 3.13: PASS;
- Trading Runner tests: `110` per version, no failures/errors/skips;
- Ruff format/lint: PASS;
- strict MyPy: PASS (`19 source files`);
- compile, sdist/wheel, clean-wheel import and paper-only audit: PASS.

Artifacts:

- Python 3.12: `8629736129`, `sha256:509b15b2acb0887a2bf362ca41866a3852a8336448031fbcdd4ead2217bffa5d`;
- Python 3.13: `8629732844`, `sha256:08b98deefad542f9c0ba8b215d243db7560baa9057bc35bc02a47aaf034c172d`.

### Central v0.6 executable acceptance

- run: `30194713641`;
- same exact candidate;
- Python 3.12 and 3.13: PASS;
- recorded JUnit total: `661` tests per version, no failures/errors/skips;
- frozen Core, Setup → Entry, cross-layer/intrabar, complete Paper, Position & Exit and complete Trading Runner gates: PASS;
- package installation, dependency check, Runner build/twine and evidence hashing: PASS.

Artifacts:

- Python 3.12: `8629736641`, `sha256:05332152aab2c848f3cc680283d8193178fddbe20ba666b59b73bb36e60fd0cd`;
- Python 3.13: `8629736613`, `sha256:ce77a5302af7b59d4d01fc419396642b69bfc5afbbb3c616efa42f6ad9520508`.

## Acceptance conclusion

`ACCEPTANCE PASS / FINAL AUDIT PASS`.

The clean-room Trading Runner is a verified paper-only v0.6 integration candidate. This matrix does not authorize merge, auto-merge, tag, release, deployment, credentials, package publication or live execution. Draft PR #197 remains pending explicit human authorization.
