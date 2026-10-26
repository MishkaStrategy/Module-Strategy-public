# Modular Strategy v0.6 — Position & Exit Management checkpoint

Status: PUBLISHED FROM EXACT-HEAD FAST AND HEAVY RELEASE EVIDENCE

## Identity

- Repository: `mishkacher/Module-Strategy`
- Central integration branch: `integration/modular-strategy-v0.6.0-rc`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Starting integration checkpoint: `d7046b2407085c44d7152ee6e63767f77d735169`
- Frozen verified runtime source: `75d2688840e003925d699555aa28739d9713bad7`
- Exact tested component head: `b4f414b264f8c4cf1d14d0ebcda83fe0bb1d8dd7`
- Accepted live head: `8ba3396068efd0ba2252c3b488be5d296f4f441a`
- Ordinary two-parent merge candidate: `f5df43b594988622c2741e40264d3dbca9ba6ad7`

The merge candidate has the exact Paper Execution checkpoint as its first parent and the complete Position & Exit history as its second parent. It was created on the temporary candidate branch only; no merge targeted `main` or frozen Core.

## Post-evidence delta

Exact tested head → live head:

- ahead by one commit;
- behind by zero commits;
- changed file: `.github/workflows/position-exit-management-ci.yml` only;
- product source, tests, package metadata and public contracts unchanged.

The accepted workflow-only delta routes ordinary and release jobs through the current workload-role policy without binding to a physical machine identity.

## Component boundary

Position & Exit Management 0.2.1 deterministically manages an already-open factual net position:

- maximum holding-time exits;
- staged partial exits and residual-only retries;
- break-even and R/percent trailing;
- monotonic protective-stop updates;
- bounded orphan-order cleanup;
- checkpoint/restore and deterministic replay.

It consumes completed candles plus factual position/order/execution snapshots and emits typed close, stop-update, cancellation and status decisions. It does not:

- create entries or size initial positions;
- communicate with an exchange;
- round to instrument rules;
- mutate portfolio state directly;
- implement production OCO;
- duplicate Core DTOs, registry, Feature Store, Paper execution state or reconciliation ownership.

Execution, fills and factual reconciliation remain Paper Execution/Core responsibilities. Exit decisions have priority over new additions in the surrounding orchestrator.

## Exact-head permanent CI

Permanent workflow `.github/workflows/position-exit-management-ci.yml`.

Pull-request run `29909306339` at exact tested head:

- Python 3.12 fast quality: SUCCESS;
- Python 3.13 fast quality: SUCCESS;
- heavy job intentionally skipped for PR event;
- workflow conclusion: SUCCESS.

Push-only release run `29909303667` at the same exact head:

- Python 3.12 fast quality: SUCCESS;
- Python 3.13 fast quality: SUCCESS;
- heavy release audit on macOS ARM64: SUCCESS;
- aggregate checkpoint reporter and enforcement: SUCCESS.

## Verified gates

- compileall: PASS;
- Ruff lint and format: PASS;
- strict MyPy: PASS;
- component tests: 288 passed;
- branch-aware coverage: 92%;
- public import, entry point, registry and checkpoint/restore smoke: PASS;
- full repository suite: 244 passed;
- installed-sdist suite: 287 passed;
- Bandit: PASS;
- detect-secrets: zero findings;
- pip-audit: zero known vulnerabilities in auditable dependencies;
- CycloneDX SBOM: PASS;
- Core wheel and module wheel/sdist: PASS;
- Twine and release-artifact verification: PASS;
- clean wheel-only installation and `pip check`: PASS;
- installed-sdist wheel rebuild: PASS;
- deterministic replay across hash seeds: PASS;
- 50,000-event no-op benchmark: about 11.4k events/s median with bounded checkpoint/memory evidence;
- 250,000-event active restart benchmark: about 48.5k events/s median with deterministic restored state.

Retained artifacts:

- Python 3.12 digest `sha256:4a57ff61ec168d188829aa98b588aca66ab6fbc6904a7af735c14ceb6391021c`;
- Python 3.13 digest `sha256:9945926a6d6856911e900ddfe10e2a8f7c72eaac0e4b93a67d051f960fed4b5b`;
- full release digest `sha256:966188e965b7b69b6d8d532d3a3286b674649eb5999c2efe22fd4336674d935a`.

GitHub-hosted `core-ci` and `integration-ci` runs that terminated before executable steps are recorded as infrastructure/account failures, not as component test outcomes. The dedicated self-hosted exact-head gates completed successfully.

## Integration ownership

The final diff is restricted to:

- `modules/position_exit_management/**`;
- `.github/workflows/position-exit-management-ci.yml`;
- `.github/actionlint.yaml`.

The 4-line actionlint configuration declares only the movable `fast` and `backtest` self-hosted labels. It does not encode OS, architecture, runner name or machine identity.

## Known limitations

- OHLC cannot reconstruct intrabar ordering;
- exchange rounding, fills, OCO and reconciliation remain adapter responsibilities;
- late first attachment after original risk is lost cannot reconstruct R, so R-dependent actions fail closed;
- one mutable instance serves one `(symbol, timeframe)` stream;
- the runtime must preserve monotonically increasing deterministic event indices across restore;
- correctness does not establish strategy profitability or whole-system production-live readiness.

## Safety state

- PR #168 remains open and Draft;
- no force-push, rebase or history rewrite;
- no merge to `main` or frozen Core;
- no tag, release, package publication or live execution.

## Next executable component

Backtest Engine & Simulation must be located and exact-head classified. Integration must preserve the current execution semantics, deterministic event ordering, conservative intrabar rules, funding/cost attribution and reproducibility without reusing Paper's mutable runtime state as a second source of truth.
