# Position & Exit Management

Independent stateful module for Modular Strategy Core that manages an already-open net position.

## Responsibilities

The module owns only:

- maximum holding-time exit;
- staged partial exits based on initial R;
- residual-only retry after incomplete partial fills;
- breakeven stop movement;
- R-based or percent-based trailing;
- monotonic protective-stop updates that preserve the current take profit;
- cancellation of remaining reduce-only, setup-related or explicitly tagged orders after flat is confirmed;
- deterministic checkpoint/restore and point-in-time management status.

It does **not** create entries, calculate the initial stop or target, size positions, round to exchange rules, communicate with an exchange, mutate another module or access future data.

## Public API

```python
from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitConfig,
    PositionExitReason,
    STATUS_FEATURE_KEY,
)
```

Entry point:

```text
modular_strategy.modules:position_exit_management
```

Manifest:

- name: `position_exit_management`;
- version: `0.2.1`;
- Module API: `1.0`;
- state schema: `2`;
- required features/timeframes: none;
- produced feature: `position_exit_management.status`.

## Inputs

The module consumes only the public closed-bar `MarketView`:

- `current` completed candle;
- `position` immutable `PositionSnapshot`;
- `pending_orders` immutable order snapshots;
- `exit_pending`;
- `data_gap`;
- deterministic engine index.

One module instance is bound to one `(symbol, timeframe)` stream. A stream change while a position or cleanup context exists fails closed. The adapter or engine must preserve a monotonically increasing deterministic `MarketView.index` across checkpoint restore and process restart because bounded cancellation retry state stores the last attempted event index.

## Outputs

The module returns standard `ModuleDecision` fields:

- `ClosePositionRequest` for full or partial exits;
- `PositionUpdateRequest` for stop replacement while preserving take profit;
- `CancelRequest` for orphan cleanup;
- structured namespaced skips and diagnostics;
- opt-in point-in-time `position_exit_management.status` feature (`emit_status_feature=true`).

The execution layer owns actual fill timing, quantity/tick normalization, OCO behavior and reconciliation.

## Temporal semantics and no-look-ahead

- `Candle.timestamp` is bar open time; decisions use the derived completed `close_time`.
- Only `MarketView.current` and past state are read.
- A decision emitted from bar N is executed by Core no earlier than the next eligible execution step.
- Exact duplicate bars are idempotent.
- Conflicting duplicates and out-of-order events fail closed.
- A gap suspends new price-driven partial, breakeven and trailing activation and cannot update the favorable extreme.
- A gap does not block a due time exit or the quantity-only residual retry of a partial close that was activated on a prior valid bar.
- `exit_pending` remains higher priority than any retry.
- Future entry timestamps fail closed.

## R and partial-exit semantics

Initial risk per unit is captured from the first valid stop observed for the position:

```text
LONG:  entry_price - initial_stop
SHORT: initial_stop - entry_price
```

Every partial fraction is calculated from the original position quantity, not the remaining quantity. A later stage cannot start until the previous target quantity is observed. If only part of a close request is filled, the next request contains only the residual amount. A near-full partial that would round to the full factual quantity is clamped to the next lower representable float while retaining its exact positive target in checkpoint state. ULP tolerance may confirm completion only after an observed factual quantity decrease; an unrepresentable subnormal partial fails closed without marking the stage complete.

## Breakeven and trailing

Breakeven supports an R offset. Trailing modes:

- `r_multiple`: distance from the favorable extreme is a multiple of initial risk;
- `percent`: distance is a percentage of the favorable extreme.

Stops are direction-aware, monotonic, strictly behind the completed-bar close and must improve by at least `minimum_stop_move_bps`. The final candidate is revalidated after float transport; a Decimal candidate that collapses onto or through the market float is not emitted and does not activate protective runtime flags.

## Configuration example

```yaml
modules:
  - plugin: position_exit_management
    enabled: true
    parameters:
      max_holding_minutes: 720
      partial_exits:
        - trigger_r: 1.0
          fraction: 0.25
        - trigger_r: 2.0
          fraction: 0.25
      breakeven_trigger_r: 1.0
      breakeven_offset_r: 0.0
      trailing_mode: r_multiple
      trailing_activation_r: 2.0
      trailing_distance_r: 1.0
      trigger_source: close
      minimum_stop_move_bps: 1
      market_buffer_bps: 2
      cancel_retry_bars: 1
      max_tracked_cancels: 1024
      max_cancel_requests_per_bar: 64
```

## Routine monitoring performance

When all management actions, verbose status output and idle skips are disabled, stable position bars use a bounded O(1) fast path. Duplicate/out-of-order checks, future-entry protection, position identity, scale-in policy and checkpoint updates remain active. Active partial/BE/trailing/time-exit configurations do not enter this path.

## State and restart

State schema 2 stores:

- canonical config fingerprint;
- stream identity;
- last accepted event cursor and fingerprint;
- initial position geometry and quantity;
- completed/pending partial stages;
- favorable extreme and protective flags;
- bounded cancel-attempt tracking.

Restore is transactional. Unknown, missing, oversized, noncanonical, incompatible-version or incompatible-config state is rejected before the active module is mutated. Decimal text length is bounded before parsing. Restored timestamps must exactly match the UTC `datetime.isoformat()` representation emitted by the module. An initial stop must define positive initial risk; legacy schema-2 event fingerprints remain accepted as a fail-closed duplicate barrier. The surrounding runtime must resume the deterministic event index monotonically; resetting it while restoring module state can intentionally defer cancellation retries until the restored index domain is reached.

## Gap, duplicate and cleanup behavior

- exact duplicate: ignored without a repeated action or feature;
- conflicting duplicate: rejected;
- out-of-order event: rejected;
- flat state: matching orphan orders are cancelled in deterministic order;
- cancellation attempts are rate-limited and bounded;
- setup context is retained until setup-only orphan orders disappear.

## Known limitation

Core 0.4.x stores `initial_risk_amount` on its mutable internal position but does not expose initial stop/risk through `PositionSnapshot`. A normal restart is safe because this module checkpoints the value. A first-time late attachment after the stop has already moved through entry cannot reconstruct original R from public data and therefore safely disables R-based actions. A separate Core Change Request documents the optional contract improvement; the module does not modify Core.

## Hardening verification

The package includes unit, negative, property-based, adversarial, checkpoint, real-Core integration, deterministic replay, load, wheel, sdist and entry-point tests.

Frozen runtime source commit `75d2688840e003925d699555aa28739d9713bad7` was verified in an isolated Mac hardening run:

- 288 module scenarios passed;
- branch-aware coverage remained 92%;
- Ruff formatting/lint and strict mypy passed;
- focused regressions cover causal ordering, bounded Decimal input, tiny and near-full partial transport, factual partial confirmation, canonical restored state, gap-safe partial retry and representable stop geometry.

The previous complete remote release baseline at commit `599ca262e83ef9953f48550b4bb12b21b47e84e9` passed Python 3.12/3.13 fast gates, 268 module tests, 244 repository tests, 267 installed-sdist tests, security/SBOM, packaging, clean-install, replay and benchmark gates. The final connector-authored documentation synchronization head must repeat those permanent release gates before exact-head PR evidence is refreshed.

Status: `RELEASE GATES RUNNING — KEEP DRAFT; MERGE/RELEASE NOT AUTHORIZED`.
