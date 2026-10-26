# Round 12 — actual HTF close time and look-ahead protection

## Baseline

Core-generated higher-timeframe candles derived close time only from the timeframe label. A locally aligned calendar day spanning a DST transition could contain 23 or 25 completed hourly bars while claiming a fixed 24-hour close. Input validation also allowed overlapping bar intervals, and run identity ignored the actual close boundary.

## Change

- immutable `AggregatedCandle` carries the actual close of consumed base data;
- fixed-ratio and calendar-aligned aggregation stamps the final base-bar close;
- parseable output duration must match ratio and base interval;
- input candle intervals cannot overlap;
- actual close time participates in data hash and run identity;
- spring/fall DST and no-early-publication regression tests.

## Compatibility

The public base `Candle` constructor and existing positional call sites remain unchanged. Core-generated HTF values remain `isinstance(..., Candle)` and expose a more accurate `close_time` property.
