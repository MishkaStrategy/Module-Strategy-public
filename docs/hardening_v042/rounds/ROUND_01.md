# Round 01 — strict mathematical boundaries

- Goal: ensure Python booleans cannot silently become numeric `0/1` in risk, fee, price or quantity calculations.
- Baseline: `float(True) == 1.0` was accepted by shared numeric validators.
- Change: shared finite/positive/nonnegative validators now reject `bool` and produce a clear type failure for non-numeric values.
- Tests: bool matrix plus existing NaN/inf boundaries; full repository suite.
- No-look-ahead/checkpoint/contracts: unaffected; valid numeric callers are unchanged.
