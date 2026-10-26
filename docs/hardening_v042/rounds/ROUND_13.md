# Round 13 — atomic multi-timeframe aggregation

## Baseline

`TimeframeStore.push()` invoked aggregators sequentially. If an early aggregator accepted and mutated its buffer but a later aggregator rejected the same candle because of stream identity or ordering, the store was left in a partially advanced state.

## Change

- every aggregator exposes a mutation-free `validate_push()` boundary;
- the store preflights the candle against all configured aggregators first;
- only after every preflight succeeds are buffers, cursors, gap counters and histories updated;
- typed aggregate construction avoids ambiguous dictionary unpacking at the strict-MyPy boundary;
- regression tests prove rejected events preserve every timeframe history and aggregation count.

## Result

A base event is now accepted by all timeframe contexts or by none. Multi-timeframe state cannot diverge because of a late validation failure.
