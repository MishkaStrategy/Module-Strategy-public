# Breakout–Return feature producer identity contract

Date: 2026-07-21

Component: `strategy_modules.breakout_return.BreakoutReturnModule`

## Problem

A Feature Store may contain multiple active features with the same key, symbol and timeframe but different `source_module` values. Before this hardening, feature-mode Breakout–Return selected the latest matching key without an optional producer constraint. A later foreign producer could therefore replace the intended causal range even when the strategy configuration expected a specific upstream module.

This was not a look-ahead issue: Feature Store point-in-time filtering remained correct. It was an input ownership and cross-producer collision issue.

## Parameter

```python
reference_feature_source_module: str | None = None
```

Rules:

- `None` preserves the existing producer-agnostic behaviour;
- a configured value must be a nonblank trimmed string;
- the option is valid only with `reference_mode="feature"`;
- the value is declared in `manifest.parameters_schema`;
- the value is exposed in public `parameters` when configured;
- the value participates in the deterministic configuration fingerprint;
- checkpoint state from a differently bound producer is rejected before restore mutation.

## Selection semantics

With a producer configured, Breakout–Return resolves a reference only from features matching all applicable constraints:

- `reference_feature_key`;
- current symbol;
- optional `reference_feature_timeframe`;
- `reference_feature_source_module`;
- Feature Store point-in-time availability and expiry;
- optional schema version;
- optional maximum age.

A matching key produced only by another module is treated as missing. It is never silently substituted.

## Active-setup lifecycle

Producer identity remains part of the reference contract after a setup is armed:

- with `range_change_policy="invalidate"`, disappearance of the intended producer invalidates the active setup even if a foreign producer publishes a replacement with the same key;
- with `range_change_policy="keep_snapshot"`, the frozen intended range remains authoritative and a foreign replacement is ignored.

This preserves the existing range-policy semantics while preventing ownership substitution.

## Example

```yaml
modules:
  - class: strategy_modules.breakout_return:BreakoutReturnModule
    parameters:
      reference_mode: feature
      reference_feature_key: session_range.completed
      reference_feature_source_module: session_range
      reference_feature_timeframe: 5m
      reference_feature_schema_version: "1"
      max_reference_age_seconds: 86400
```

The parameter does not import Session Range or any other concrete module. It binds only an immutable feature producer identity string and therefore preserves replaceability and the Module API boundary.

## Backward compatibility

Default construction omits the new public parameter and returns the original base configuration fingerprint. Existing producer-agnostic configurations and checkpoints remain unchanged.

Configurations that explicitly bind a producer receive a distinct fingerprint. This prevents a checkpoint created for one upstream producer from being restored under another producer contract.

## Regression evidence

`tests/breakout_return/regression/test_feature_source_contract.py` verifies:

1. the configured producer is selected even when a later foreign feature has the same key;
2. foreign-only features are treated as `breakout_return.reference_missing`;
3. invalid, whitespace-surrounded and rolling-mode producer configuration fails before processing;
4. manifest schema and public parameters expose the contract;
5. different producer bindings have different configuration fingerprints;
6. cross-producer checkpoint restore fails closed;
7. intended-producer loss invalidates an active setup under `invalidate` despite a foreign replacement;
8. `keep_snapshot` retains the frozen intended setup and ignores the foreign replacement.

Exact self-hosted run `29838403367` at evidence parent head `cc211b4801650f70937272c26a681e2d75b1e07e` passed:

- 131 Breakout–Return tests on Python 3.12;
- 131 Breakout–Return tests on Python 3.13;
- 312 full repository tests on Python 3.13;
- statement coverage 90.856031%, displayed as 91%, gate 90%;
- Ruff, strict mypy, compileall, packaging, clean installation, security and deterministic benchmark gates.