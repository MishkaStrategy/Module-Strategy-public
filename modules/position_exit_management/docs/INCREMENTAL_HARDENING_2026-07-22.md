# Incremental Hardening Report — 2026-07-22

## Scope

This report records the continuation of the Position & Exit Management 0.2.1 hardening program on branch `modules/position-exit-management-hardening`, based on `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`.

The continuation did not redesign the module, change Module API 1.0, change state schema 2, move execution responsibility into the module or modify Core. The existing algorithm remains the internal engine; one hardened public class is exposed through the package, entry point and historical direct import path.

## Confirmed findings and corrections

### Causality and event ordering

- A future-dated position could be rejected only after stream/cursor mutation. The rejection now occurs before mutation and the corrected event for the same bar remains processable.
- The early future-position guard preserves the established priority of out-of-order and conflicting-duplicate rejections.
- An entry bar could use a high or low formed before the actual fill. For bars containing the fill timestamp, favorable extreme and R triggering use only the completed close until a fully post-entry bar is available.
- Empty or malformed `base_bars`, non-candle current values, unrepresentable candle close times and event indices outside the bounded counter domain fail before stream binding or fingerprint construction.

### State and restart safety

- Checkpoints with completed or pending partial stages but no initial risk are rejected.
- R-activated percent trailing cannot be restored without initial risk.
- Active position entry time cannot exceed the saved event cursor.
- Oversized `bars_managed` and cancellation-attempt counters are rejected transactionally.
- Decimal state fields use bounded canonical serialization and restore independently of the ambient process Decimal context.
- Oversized Decimal text is rejected before invoking the Decimal parser.
- Restored timestamps must exactly match the canonical UTC `datetime.isoformat()` representation emitted by the module.
- A stored initial stop must define positive initial risk; semantically impossible stop-at-entry or wrong-side checkpoints are rejected atomically.
- Existing schema-2 legacy event fingerprints remain accepted as fail-closed replay barriers; no incompatible schema migration was introduced.
- The integration contract explicitly states that the surrounding engine or adapter must preserve a monotonically increasing deterministic `MarketView.index` across restore because cancellation retry state stores event indices.

### Numerical and transport safety

- Position quantity, entry price, stop and target must fit finite positive Core float DTO transport.
- A protective stop that cannot be represented as a finite float is not emitted.
- A Decimal protective stop that collapses onto or through the completed-bar market after float transport is not emitted and cannot activate protective runtime flags.
- A near-full partial whose exact target remains positive is encoded below the factual position quantity with `math.nextafter` instead of becoming an accidental full close.
- Partial retry uses the same representable-quantity rule and preserves the exact checkpoint target.
- ULP tolerance may confirm a pending partial only after factual quantity is observed to decrease; unchanged subnormal quantity cannot silently complete a stage.
- A partial that cannot encode any positive float request fails closed and leaves the stage uncompleted.
- Extreme R diagnostics are retained as exact decimal strings instead of JSON `Infinity`/`NaN` values.
- Decimal precision and exponent bounds apply to configuration, checkpoints and public status payloads.
- Configuration fingerprints no longer use context-sensitive `Decimal.normalize()`.
- Runtime calculations and restore run under a private deterministic Decimal context.
- Partial-fraction validation uses an extended exact-validation context and rejects totals above one even at the supported exponent boundary.

### Configuration and resource safety

- Maximum holding time cannot exceed the `timedelta` domain.
- Partial-exit configuration is limited to 64 stages and an infinite iterable fails after the bound instead of being materialized indefinitely.
- Retry, tracking, request and event counters use a signed 64-bit non-negative domain.
- Direct `PositionExitConfig` construction validates enum instances and typed partial rules explicitly.
- Parameter conversion accepts only the declared `int | float | str | Decimal` domain and rejects arbitrary objects before invoking user-defined stringification.
- Oversized Decimal configuration strings are rejected before conversion.
- The machine-readable manifest reports the same holding, partial-stage and counter limits enforced by runtime validation.

### Gap and execution-reconciliation safety

- Corrupt/gap OHLC cannot activate a new partial stage, breakeven, trailing update or favorable-extreme change.
- A residual retry for a partial stage activated on a prior valid bar is quantity-only execution reconciliation and remains actionable on a gap bar.
- `exit_pending` and due time exits retain higher priority than gap-safe partial retry.
- The decision reports `DATA_GAP` alongside the retry so observability does not hide the invalid candle.

### Observability and public API

- A factually confirmed partial stage emits `PARTIAL_EXIT_CONFIRMED` status when no higher-priority action is produced, without repeating the close request.
- Package import, entry-point loading and direct `modular_strategy_position_exit_management.module` import resolve to the same hardened public class and manifest.

## Regression coverage added

The incremental program added 65 focused regression scenarios beyond the inherited 223-test baseline:

- the original continuation contributed 45 cases across 12 files;
- the final independent runtime passes contributed 20 more cases for declared numeric types, pre-parse Decimal bounds, tiny and near-full partial transport, factual ULP confirmation, canonical state, initial-stop geometry, gap-safe retry and representable protective stops.

Frozen runtime source commit `75d2688840e003925d699555aa28739d9713bad7` completed 288 module scenarios with 92% branch-aware coverage, Ruff and strict mypy in an isolated Mac hardening workflow. Its validation artifact was uploaded before scope-enforced publication.

## Remote release evidence

The previous complete remote release baseline at exact commit `599ca262e83ef9953f48550b4bb12b21b47e84e9` passed:

- pull-request workflow `29895841117`;
- push-only release workflow `29895838773`;
- Python 3.12 and 3.13 fast quality gates;
- 268 module tests at 92% branch-aware coverage;
- 244 full-repository tests;
- 267 installed-sdist tests;
- Ruff, strict mypy, compileall, Bandit, detect-secrets and pip-audit;
- CycloneDX SBOM generation;
- Core wheel and module wheel/sdist build, Twine, release-artifact verification, clean wheel installation and `pip check`;
- deterministic replay and benchmark gates.

A later superseded connector-authored head also proved that the permanent workflow, VDS profiles, Mac heavy audit and aggregate reporter execute successfully. The final connector-authored documentation/evidence synchronization head must still repeat those gates for the frozen runtime source before its exact run IDs and artifact digests replace the historical baseline in the PR checkpoint.

GitHub-hosted Core and Integration jobs that terminate before executable steps remain classified as infrastructure/account failures, not module failures or passes. The dedicated self-hosted workflow is the component release evidence source.

The permanent workflow was validated with pinned `actionlint 1.7.12`. Heavy-run temporary paths are defined at step scope through `RUNNER_TEMP` and `GITHUB_ENV`, avoiding unsupported job-level `runner.temp` expressions.

## Release decision

Runtime source hardening is complete and isolated gates are green. The component remains:

`RELEASE GATES RUNNING — KEEP DRAFT`.

Do not merge, tag or create a release until the final connector-authored exact head completes both VDS fast profiles and the Mac release audit and an explicit combined multi-module integration decision is authorized.
