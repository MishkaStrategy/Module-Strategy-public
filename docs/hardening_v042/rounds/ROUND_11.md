# Round 11 — strict event-time and stream boundaries

## Baseline

The public timeframe aggregator accepted booleans as integers, non-positive intervals, duplicate or out-of-order timestamps and mixed symbol/timeframe streams. Public reset retained its timestamp cursor.

## Change

- strict parameter types and positive interval validation;
- duplicate and out-of-order events fail closed;
- one symbol and base-timeframe identity per active aggregator;
- public reset clears bucket, cursor and stream identity;
- internal gap reset clears only the incomplete bucket.

## Measurement

The exact-head gate covers Ruff, strict MyPy, temporal/data regression tests and the complete Python 3.12/3.13 recovery matrix.
