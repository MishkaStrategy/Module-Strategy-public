# Round 14 — gap and bracket policy regression coverage

## Goal
Lock conservative gap-through-stop behavior and deterministic stop/target ambiguity policies for both position directions.

## Result
Added adversarial short-gap and dual-policy regression tests. Stop gaps execute at the adverse open; ambiguous candles obey the explicit `intrabar_priority` setting.

## Contracts
No public contract changed.
