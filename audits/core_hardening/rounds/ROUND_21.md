# Round 21 — measured event-loop profiling

A cProfile baseline on 20,000 bars × 10 no-op modules identified repeated view construction, Candle close-time parsing, readiness construction and redundant immutable mapping copies as the dominant hot path.

Optimizations:
- cached timeframe duration parsing;
- one base history boundary, timeframe mapping and session view per bar;
- readiness reuse for modules with identical requirements;
- no repeated copy of an already immutable timeframe map.
