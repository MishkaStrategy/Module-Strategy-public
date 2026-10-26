# Canonical strategy owner handoff

## Purpose

This document is the only accepted input shape for resolving the open decisions in issue #312 and Draft PR #316. It prevents prose-only approvals from being mistaken for executable trading semantics.

The named composition remains fail-closed until every required field is supplied by an authoritative owner decision, committed to GitHub, reviewed, and validated at one exact head.

## Identity

- Composition: `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`
- Decision queue: `canonical-strategy-decision-queue-v1.json`
- Recovered-gap composition: `canonical-fvg-rt-final-15m-frozen-1h-4h.recovered-gap-v2.json`
- Recovery registry: `module-contracts-recovery-v2.json`
- Control evidence API: `strategy_audit_validation.load_control_evidence_manifest`
- Current status: `PARTIAL_RECOVERY_FAIL_CLOSED`

## Submission rules

An owner decision must:

1. identify the decision ID;
2. provide every requested value explicitly;
3. define inclusivity for every threshold;
4. define completed-candle and first-execution-bar semantics;
5. identify the authoritative source or state that the value is a new owner decision;
6. include the approving GitHub identity and UTC timestamp;
7. be committed or attached to issue #312 before implementation;
8. never use PR #298 as Frozen 4H Short.

`unknown`, `same as usual`, `standard`, `approximately`, and inferred defaults are not accepted executable values.

## STRATEGY-312-01 — remaining Frozen 1H conventions

Required decision fields:

```json
{
  "decision_id": "STRATEGY-312-01",
  "fvg_geometry": {
    "bearish_condition": "<exact formula>",
    "equality_included": false,
    "zone_lower": "<exact boundary>",
    "zone_upper": "<exact boundary>"
  },
  "short_location": {
    "formula": "<exact formula>",
    "lookback_completed_1h_candles": 8,
    "minimum": 0.75,
    "minimum_inclusive": true,
    "zero_range_behavior": "<reject or exact fallback>"
  },
  "ema50_falling": {
    "formula": "<exact comparison>",
    "lookback_completed_1h_candles": "<integer>"
  },
  "execution": {
    "first_eligible_bar": "<exact lower-timeframe bar>",
    "fill_condition": "<exact touch or penetration convention>",
    "same_bar_stop_target_priority": "<exact order>",
    "gap_through_stop": "<exact fill rule>"
  },
  "no_progress": {
    "check_time": "6h after fill",
    "mfe_formula": "<exact formula>",
    "fill_bar_included": false,
    "check_bar_event_priority": "<exact order>"
  },
  "cost_and_sizing": {
    "inherit_from": "<exact contract ID or explicit values>",
    "overrides": {}
  },
  "checkpoint_and_restart": {
    "schema_version": "<version>",
    "identity_fields": ["<fields>"],
    "pending_order_restore": "<exact behavior>",
    "open_position_restore": "<exact behavior>"
  },
  "approved_by": "<GitHub login>",
  "approved_at": "<UTC ISO-8601>",
  "source": "<authoritative path, commit, or new owner decision>"
}
```

Already recovered and not requested again unless intentionally superseded:

- bearish 1h direction;
- FVG size `0.08–1.25 ATR`;
- bearish B body `>=0.40 ATR`;
- volume `>=0.85 × SMA20`;
- impulse efficiency `>=0.45`;
- no RSI;
- close below falling EMA50;
- completed 4h close below completed 4h EMA200;
- eight-candle location threshold `>=0.75`;
- ATR/close `<=2.5%`;
- entry `C.high + 0.75 × (A.low − C.high)`;
- stop max of five completed 1h highs plus `0.05 ATR`;
- maximum stop distance `4%`;
- target `6R`, TTL `4h`, hold `12h`, no-progress `6h / 0.50R`.

## STRATEGY-312-02 — complete Frozen 4H Short

Every field below is mandatory because no authoritative contract was recovered:

```json
{
  "decision_id": "STRATEGY-312-02",
  "module_id": "frozen_4h_short",
  "direction": "short",
  "signal_timeframe": "4h",
  "setup": {
    "definition": "<exact formula>",
    "candles": "<completed candle identities>",
    "zone_boundaries": ["<lower>", "<upper>"],
    "equality_conventions": {}
  },
  "indicators": [
    {
      "name": "<name>",
      "period": "<integer or exact rule>",
      "formula": "<exact formula>",
      "warmup": "<exact bars>",
      "completed_candles_only": true
    }
  ],
  "filters": [
    {
      "name": "<name>",
      "formula": "<exact formula>",
      "operator": "<, <=, >, >=, ==",
      "threshold": "<exact value and unit>"
    }
  ],
  "entry": {
    "formula": "<exact formula>",
    "first_eligible_bar": "<exact bar>",
    "fill_condition": "<touch or penetration>",
    "ttl": "<exact duration or bar count>",
    "cancellation": "<exact conditions>"
  },
  "stop": {
    "formula": "<exact formula>",
    "buffer": "<exact value and unit>",
    "maximum_distance": "<exact fraction>",
    "gap_through_rule": "<exact rule>"
  },
  "target": {
    "formula": "<exact formula>",
    "R_multiple": "<exact value>"
  },
  "hold": {
    "maximum": "<exact duration or bars>",
    "last_index_convention": "<exact convention>"
  },
  "no_progress": {
    "check_after": "<exact duration or bars>",
    "mfe_formula": "<exact formula>",
    "minimum_R": "<exact value>",
    "fill_bar_included": false,
    "event_priority": "<exact order>"
  },
  "cost_and_sizing": {
    "inherit_from": "<exact contract ID or explicit values>",
    "overrides": {}
  },
  "checkpoint_and_restart": {
    "schema_version": "<version>",
    "identity_fields": ["<fields>"],
    "pending_order_restore": "<exact behavior>",
    "open_position_restore": "<exact behavior>"
  },
  "approved_by": "<GitHub login>",
  "approved_at": "<UTC ISO-8601>",
  "source": "<authoritative path, commit, or new owner decision>"
}
```

The expected accepted contribution is 21 trades, but that count cannot define or reverse-engineer the rules.

## STRATEGY-312-03 — cross-component arbitration

Required fields:

```json
{
  "decision_id": "STRATEGY-312-03",
  "priority_high_to_low": [
    "<fvg_rt_final_15m | frozen_1h_short | frozen_4h_short>",
    "<second>",
    "<third>"
  ],
  "passed_signal_blocks_lower_priority_before_fill": "<true or false>",
  "maximum_active_limits": "<integer>",
  "maximum_open_positions": "<integer>",
  "scope": "<portfolio, symbol, or strategy>",
  "same_timestamp_tie_breaker": "<exact stable key>",
  "gap_invalidation_order": "<exact order>",
  "cancellation_order": "<exact order>",
  "approved_by": "<GitHub login>",
  "approved_at": "<UTC ISO-8601>"
}
```

The order stored in the recovered-gap v2 asset is a loader-required placeholder and is not an approved arbitration rule.

## STRATEGY-312-04 — runtime identities and adapters

Required fields per component:

```json
{
  "decision_id": "STRATEGY-312-04",
  "modules": [
    {
      "module_id": "<stable ID>",
      "module_version": "<semantic version>",
      "package": "<repository package>",
      "setup_adapter": "<import path and class/function>",
      "feature_inputs": ["<stable identities>"],
      "output_contract": "<stable executable-plan contract>",
      "state_schema_version": "<version>"
    }
  ],
  "approved_by": "<GitHub login>",
  "approved_at": "<UTC ISO-8601>"
}
```

## STRATEGY-312-05 — exact control evidence

The submitted JSON must pass `load_control_evidence_manifest` and reference:

- official Binance USDT-M perpetual BTCUSDT data;
- 5m base timeframe, UTC;
- immutable dataset hash and registered version;
- 283-trade Final 15m ledger and SHA-256;
- 91-trade Frozen 1H ledger and SHA-256;
- 21-trade Frozen 4H ledger and SHA-256;
- 395-trade combined ledger and SHA-256;
- pre-portfolio signal counts for all three components;
- execution-contract SHA-256;
- final semantic digest;
- exact generating commit.

Minimal shape:

```json
{
  "schema_version": 1,
  "composition_name": "FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short",
  "dataset": {
    "dataset_id": "<registered ID>",
    "version": 1,
    "dataset_hash": "<64 lowercase hex>",
    "exchange": "binance",
    "market": "usdt_m_perpetual",
    "symbol": "BTCUSDT",
    "base_timeframe": "5m",
    "timezone": "UTC",
    "start": "<UTC ISO-8601>",
    "end": "<UTC ISO-8601>",
    "row_count": 1,
    "gap_count": 0,
    "duplicate_count": 0
  },
  "execution_contract_sha256": "<64 lowercase hex>",
  "component_ledgers": [
    {
      "module_id": "fvg_rt_final_15m",
      "expected_trade_count": 283,
      "path": "<relative path>",
      "sha256": "<64 lowercase hex>"
    },
    {
      "module_id": "frozen_1h_short",
      "expected_trade_count": 91,
      "path": "<relative path>",
      "sha256": "<64 lowercase hex>"
    },
    {
      "module_id": "frozen_4h_short",
      "expected_trade_count": 21,
      "path": "<relative path>",
      "sha256": "<64 lowercase hex>"
    }
  ],
  "combined_ledger": {
    "expected_trade_count": 395,
    "path": "<relative path>",
    "sha256": "<64 lowercase hex>"
  },
  "signal_counts_before_portfolio": {
    "fvg_rt_final_15m": 0,
    "frozen_1h_short": 0,
    "frozen_4h_short": 0
  },
  "semantic_digest": "<64 lowercase hex>",
  "generated_from_commit": "<40 lowercase hex>"
}
```

Placeholder hashes or zero signal counts are not evidence; the example only defines structure.

## STRATEGY-312-06 — Final 15m version approval

Required decision:

```json
{
  "decision_id": "STRATEGY-312-06",
  "stable_module_id": "fvg_rt_final_15m",
  "stable_module_version": "<approved semantic version>",
  "supersedes": "<exact v7 contract identity>",
  "impulse_efficiency_minimum": 0.45,
  "long_regime": "last completed 4h close > completed 4h EMA200",
  "short_regime": "last completed 1d close < completed 1d EMA200",
  "approved_by": "<GitHub login>",
  "approved_at": "<UTC ISO-8601>",
  "source": "<authoritative path, commit, or new owner decision>"
}
```

## Transition gate

Only after STRATEGY-312-01 through STRATEGY-312-06 are resolved may this workstream:

1. mark module contracts complete;
2. replace the recovered-gap composition with `CANDIDATE`;
3. create executable adapters;
4. rerun governance, corruption, replay, checkpoint, Research, and Backtest validation;
5. compare exact ledgers and semantic digests;
6. propose a new accepted exact head.

No owner decision authorizes merge, deployment, credentials, production mutation, or live trading.