# Round 18 — post-cap instrument validation

Quantity normalization now revalidates min/max quantity and min/max notional after max-quantity or max-notional caps. Instrument step metadata must also be compatible with declared decimal precision, preventing directional rounding from producing an invalid exchange increment.

No existing valid instrument contract is changed.
