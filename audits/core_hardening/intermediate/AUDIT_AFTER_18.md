# Intermediate audit after rounds 17–18

## Decision
**Continue after correction.** The first test fixture used a symbol inconsistent with its market data and was corrected without weakening validation. The final implementation and full regression suite are clean.

## Confirmed defects fixed
1. Non-unit `contract_size` was omitted from sizing, notional caps, fees, initial risk, unrealized PnL and realised PnL.
2. Quantity caps were not revalidated against minimum quantity/notional constraints.
3. Incompatible step/precision metadata could produce off-grid rounded values.

## Public contracts
No breaking change. `FixedFractionalSizer.size` gained an optional keyword `contract_size=1.0`; existing callers retain identical behavior. DTO fields, imports, callbacks and event schemas are unchanged.

## Evidence
- Collected tests: 173; all passed.
- Branch coverage: 83%.
- Elapsed: 1.52s; max RSS: 53,192 KiB.
- No-op benchmark: 20,000 bars × 10 modules = 4.4549s.

## Compatibility
Default `contract_size=1.0` results are regression-identical. Existing integrated modules do not provide custom contract sizes and are unaffected.

## No-look-ahead / determinism / replay
No temporal behavior changed. Decimal exchange normalization remains deterministic.

## Residual risks
Inverse contracts require a different PnL formula and remain outside the current linear-contract model. No production readiness claim is made for them.
