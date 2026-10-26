# Liquidity Sweep Module 0.1.2

`LiquiditySweepModule` publishes confirmed swing-liquidity levels and closed-bar sweep events. It is an analytical producer: it does not size positions, calculate stops or targets, place/cancel orders, reconcile exchange state or mutate the portfolio.

## Public loading

```python
from strategy_modules.liquidity_sweep import LiquiditySweepModule
```

Entry point:

```text
modular_strategy.modules:liquidity_sweep
```

## Contracts

| Contract | Value |
|---|---|
| Module version | `0.1.2` |
| Module API | `1.0` |
| State schema | `1` |
| Feature schema | `1` |
| Level feature | `liquidity_sweep.level` |
| Sweep feature | `liquidity_sweep.event` |
| Required upstream feature | none |

## Temporal semantics

A pivot is confirmed only after every configured right-side bar has closed. Existing active levels are evaluated before newly confirmed levels are inserted, so a level cannot sweep itself on its confirmation candle.

For numeric timeframes, the selected candle close time must exactly equal its open timestamp plus the declared interval. Opaque base-feed timeframe names use Core's point timestamp convention where `close_time == timestamp`. Unsupported or overflowing timeframe arithmetic fails with a stable module error.

The module consumes only the current completed selected candle and point-in-time Feature Store snapshot. Future bars cannot change an already returned decision.

## Detection outputs

### Active level

`liquidity_sweep.level` contains deterministic identity, side, price, pivot time and confirmation time. It remains active until sweep, accepted-breakout invalidation, expiry, capacity eviction, data gap or stream reset.

### Sweep event

`liquidity_sweep.event` contains:

- deterministic event ID;
- decision time;
- source level identity and price;
- bullish/bearish direction;
- penetration bps;
- wick/body ratio and wick fraction;
- extreme and close prices;
- age since confirmation.

A valid event emits setup transitions `CREATED -> CONFIRMED`. Optional signals map a high-liquidity sweep to SELL and a low-liquidity sweep to BUY.

For executable Strategy Factory composition, `setup_adapter` profile `liquidity_sweep_v1` converts this immutable event into canonical `setup.confirmed`. The adapter preserves the existing direction and geometry, uses the sweep close as confirmation price and the sweep extreme as the retracement anchor, and deliberately does not choose an entry kind. Entry policy remains downstream in Entry Model / governed search configuration.

## Configuration

```yaml
modules:
  - plugin: liquidity_sweep
    enabled: true
    parameters:
      timeframe: 15m
      pivot_left_bars: 2
      pivot_right_bars: 2
      min_level_age_bars: 1
      max_level_age_bars: 500
      max_active_levels: 128
      min_penetration_bps: 0
      max_penetration_bps: 100
      close_reclaim_bps: 0
      invalidation_close_bps: 0
      min_wick_to_body_ratio: 1
      min_wick_fraction: 0.35
      require_directional_close: false
      emit_signals: false
```

Resolved numeric parameters are finite and canonical; signed zero does not change the configuration fingerprint. Timeframe values are normalized and bounded to representable durations.

## Input and stream behavior

- exact duplicate: idempotent wait/skip without sequence advancement;
- repeated explicit HTF candle: expected wait state;
- conflicting duplicate: rejected without mutation;
- same-stream out-of-order candle: rejected without mutation;
- symbol/timeframe identity switch: old bounded state is deleted/reset before processing the new stream;
- data gap: active levels and pivot buffer are invalidated/reset;
- one mutable module instance represents one symbol/timeframe stream at a time.

## State and restart

Checkpoint state contains:

- schema and configuration fingerprint;
- processed sequence and counters;
- bounded pivot buffer;
- bounded active levels;
- latest selected bar.

Restore accepts real Kernel deep-frozen tuple-like sequences. It validates capacities before element parsing, then validates exact nested keys, string identifiers, finite Decimals, chronology, exact timeframe duration, stream identity, deterministic feature identity, lifecycle counters and configuration compatibility before atomic assignment.

After restore:

- a Core `on_start()` preserves continuation once;
- direct `on_bar()` continuation consumes the same allowance;
- a later fresh `on_start()` resets the instance for an independent run;
- missing active-level features are republished before first accepted continuation evaluation;
- failed restore does not mutate existing state.

## Validation evidence

Exact code commit `0f7ea73e5926332b4cc9597012dd81a867e8b43a` passed self-hosted workflow `29839811718` on Python 3.12 and 3.13, including focused branch coverage, full repository tests, wheel/sdist clean execution, strict typing, security scans, dependency audit, SBOM, deterministic benchmark and a million-event segmented restart stress.

Audit documents are under `docs/audits/liquidity_sweep/`.

## Upgrade from 0.1.1

Feature keys, parameter names, Module API, state payload schema and feature schema are unchanged. The patch version records corrected runtime and validation semantics. Because the current Kernel envelope requires exact module-version equality, create a fresh Kernel checkpoint after deployment unless Core introduces an explicit compatible-version migration policy.

## Limitations

- OHLC does not reveal true intrabar ordering;
- swing levels are a price-action proxy rather than order-book liquidity;
- equal-high/equal-low pool clustering is outside this version;
- defaults do not prove trading edge;
- production/live readiness depends on the complete entry, protection, risk, execution, reconciliation and exit chain.
