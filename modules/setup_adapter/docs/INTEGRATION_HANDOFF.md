# Canonical Setup Adapter — Integration Handoff

## Scope

The adapter bridges source-specific setup confirmation features to the canonical `setup.confirmed` schema required by Entry Model 0.2.0. It does not modify Core and does not own entry policy, protection, risk, execution or position management.

## Dependency chain

```text
session_range.completed
        ↓
breakout_return.confirmed
        ↓
setup_adapter
        ↓
setup.confirmed
        ↓
entry_model.plan / entry_model.rejection
```

Core 0.4.0 dependency planning resolves this order from manifest feature contracts. Same-bar visibility is preserved because each downstream module receives a fresh point-in-time Feature Store snapshot after the previous module's decision is applied.

## `breakout_return_v1` mapping

| Breakout–Return field | Canonical field |
|---|---|
| `setup_id` | `setup_id` |
| `signal_side` | `side` |
| `confirmation_time` | `confirmed_at` |
| `return_close` | `confirmation_price` |
| `breakout_extreme` | `extreme_price` |
| source context fields | `metadata.source` |

The adapter deliberately does not set `entry_kind`. Entry Model applies its configured default unless another canonical producer explicitly supplies an entry hint.

## Geometry invariants

- BUY: `breakout_extreme < return_close`.
- SELL: `breakout_extreme > return_close`.
- SELL must correspond to an above-range breakout; BUY to a below-range breakout.
- Confirmation time, source publication time and current completed-bar close must match exactly.

## Integration configuration

Install the adapter package and Entry Model package, discover their entry points, then add the modules to the common configuration. They may be listed in any order; the dependency planner must resolve:

```yaml
  - plugin: entry_model
    parameters:
      default_entry_kind: limit_retracement
      retracement_fraction: "0.382"
      ttl_bars: 36
      allowed_entry_kinds: [limit_retracement]

  - plugin: setup_adapter
    parameters:
      sources:
        - feature_key: breakout_return.confirmed
          profile: breakout_return_v1
          source_module: breakout_return
      max_processed_sources: 10000
```

## Required integration gates

1. Dependency order is `session_range`, `breakout_return`, `setup_adapter`, `entry_model`.
2. No canonical setup or entry plan exists before the completed return bar.
3. Exactly one canonical setup and one entry plan appear on the return bar.
4. Source, canonical setup and entry plan share the same `created_at`.
5. Appending future bars does not change the emitted entry plan.
6. Checkpoint/restore preserves duplicate and conflict barriers.
7. Clean wheel installation discovers exactly one `setup_adapter` entry point.

## Downstream boundary

The executable chain still stops at `entry_model.plan`. Protection/Target, Risk & Position Sizing, order-intent/execution and Position/Exit Management modules are separate dependencies and are not implemented by this adapter.
