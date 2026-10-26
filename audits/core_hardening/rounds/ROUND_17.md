# Round 17 — contract-size-aware risk and accounting

## Defect
`InstrumentSpec.contract_size` was enforced for min/max notional checks but ignored by fixed-fractional sizing, fees, risk amount, unrealized PnL and realised PnL. Non-unit contracts therefore produced materially wrong risk and accounting.

## Fix
The existing sizer accepts an optional backward-compatible `contract_size=1.0` keyword. Broker sizing, notional caps, fees, PnL, equity and initial risk now consistently apply the instrument contract multiplier.

Default spot/linear behavior with contract size 1 is unchanged.
