# Core change requests

No breaking Core Change Request was required.

## CCR-001 — optional resource bounds (accepted, compatible)

**Evidence:** long-history and oversized-state tests demonstrated a need for deployment-selectable bounds.

**Change:** optional `DataConfig.max_base_history`, `FeatureStore.max_features` and `ModuleRegistry.max_config_bytes` controls.

**Compatibility:** all defaults preserve v0.4.0 behavior. No migration is required. No deprecation is introduced.

## CCR-002 — contract-size semantic correction (accepted bug fix)

**Evidence:** regression tests demonstrated that instruments with `contract_size != 1` produced incorrect notional, fees, risk and PnL.

**Change:** the existing field is now applied consistently; `FixedFractionalSizer.size` gained an optional `contract_size=1.0` argument.

**Compatibility:** unit-contract behavior is unchanged. Non-unit behavior changes intentionally from incorrect to correct. Existing callers need no code migration.

## CCR-003 — setup lifecycle owner correction (accepted internal fix)

**Evidence:** the downstream executor harness reproduced a strict setup-transition failure when Risk submitted an order for a setup created upstream.

**Change:** automatic order/fill transitions retain the original setup owner and record the order producer in metadata.

**Compatibility:** no public signature or DTO changed. This removes an integration blocker.
