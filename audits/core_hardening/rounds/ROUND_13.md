# Round 13 — OHLC ambiguity at entry

## Goal
Prevent same-bar bracket replay when an intrabar order has an unknown fill position in the OHLC path.

## Evidence
A limit entry and its target could both be inside one candle while the target occurred before the entry. The previous opt-in `allow_exit_on_entry_bar` replayed the entire range and could create an impossible exit.

## Change
Same-bar bracket evaluation remains opt-in, but is now restricted to market entries executed from the next bar open. Limit, stop, stop-limit and take-profit entries defer bracket processing until the next candle.

## Contracts
No public DTO, import path, callback or feature contract changed.
