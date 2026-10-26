# Canonical Setup Adapter 0.1.0

A strategy-independent Module API 1.0 adapter that converts source-specific confirmed setup features into the canonical `setup.confirmed` schema consumed by Entry Model.

Built-in profiles:

- `breakout_return_v1` — maps the existing `breakout_return.confirmed` payload without changing its trading semantics.
- `canonical_setup_v1` — accepts an already canonical family setup payload and republishes it as trusted `setup.confirmed` with authoritative adapter/source provenance.
- `liquidity_sweep_v1` — maps the deterministic `liquidity_sweep.event` fact into canonical setup schema without choosing entry policy.
- `fvg_rt_final_15m_v1` — validates the owner-bound Final 15m FVG-RT snapshot and publishes its explicit LIMIT entry plus exact per-setup cutoff while preserving stop/target/exit provenance for the downstream parity work.

The shared schema is defined by `build_canonical_setup_payload(...)` and validated again after every profile adapter. This keeps the downstream Entry Model family-neutral while profile-specific code owns only the translation from its source signal/setup semantics.

The adapter does not choose risk, protection, size, execution, or position-management policy. It validates source causality and canonical payload safety, publishes deterministic canonical features, emits typed rejection features for invalid inputs, and preserves bounded replay state across checkpoints.

## Install

```bash
python -m pip install modular_strategy_core-0.4.0-py3-none-any.whl
python -m pip install modular_strategy_entry_model-0.2.0-py3-none-any.whl
python -m pip install modular_strategy_setup_adapter-0.1.0-py3-none-any.whl
python -m pip check
```

## Source-specific mapping

```yaml
modules:
  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: breakout_return.confirmed
          profile: breakout_return_v1
          source_module: breakout_return
      max_processed_sources: 10000
```

## Liquidity-sweep mapping

```yaml
modules:
  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: liquidity_sweep.event
          profile: liquidity_sweep_v1
          source_module: liquidity_sweep
```

The profile derives BUY/SELL from the existing bullish/bearish sweep direction, binds the event to the current source bar, uses sweep close as confirmation price and sweep extreme as the optional retracement anchor. It does not set `entry_kind`; Entry Model or Strategy Factory configuration remains the owner of market/confirmation/retracement choice.

## FVG-RT Final 15m mapping

```yaml
modules:
  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: fvg_rt_final_15m.snapshot
          profile: fvg_rt_final_15m_v1
          source_module: fvg_rt_final_15m
```

The profile is fail-closed on module/version identity, completed-timeframe and condition sets, historical signal-key provenance, explicit LIMIT/stop geometry, target-R, TTL, max-hold and no-progress parameters. It publishes the exact explicit LIMIT entry, `entry_ttl_bars`, stop/target geometry and max-hold/no-progress lifecycle envelope for downstream transport. Position-exit execution semantics remain part of the later FVG-001 composition work. The partial Final 15m path is intentionally not admitted to the trusted Strategy Factory catalog.

## FVG-RT Frozen 1H / 4H mapping

```yaml
modules:
  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: frozen_1h_short.snapshot
          profile: fvg_rt_frozen_1h_v1
          source_module: frozen_1h_short
        - feature_key: frozen_4h_short.snapshot
          profile: fvg_rt_frozen_4h_v1
          source_module: frozen_4h_short
```

Both profiles fail closed on exact module/version/timeframe identity, condition and completed-timeframe sets, historical signal-key digest, stop-distance ceiling and immutable execution windows. They preserve the raw `historical_priority` and also encode Router-compatible `priority=-historical_priority`, because the historical composer treats lower values as stronger while the Router treats higher values as stronger. They publish exact `entry_ttl_bars`, stop/target and max-hold/no-progress lifecycle metadata. Entry Model may use a family ceiling of 576 five-minute bars; the upstream TTL hint can shorten that ceiling to each canonical 36/48/576-bar lifetime but cannot extend it. Full Position Exit execution and Strategy Factory admission remain later FVG-001 work.

## Canonical-family handoff

A family module may emit its own namespaced confirmation feature using canonical setup schema 1 and let the adapter bind authoritative provenance:

```yaml
modules:
  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: my_family.confirmed
          profile: canonical_setup_v1
          source_module: my_family
```

The canonical profile is intentionally policy-free. The source payload must already declare the setup side, confirmation price/time and any explicit entry hint required by Entry Model. Unknown fields, stale/future confirmations, malformed prices, symbol drift and forged reserved provenance fail closed.

The Core dependency planner places the adapter after the source module and before Entry Model from feature requirements alone. No Core strategy branch is required.

See `docs/INTEGRATION_HANDOFF.md` for the original breakout-return mapping and integration gates.
