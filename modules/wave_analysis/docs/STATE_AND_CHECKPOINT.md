# State Schema and Checkpoint

State schema remains version `1` for RC2. RC1 checkpoints are supported.

## Root invariants

The checkpoint contains exact known fields, module/config fingerprints, counters, base-event identity, symbol binding, and one state mapping for every configured timeframe. Unknown or missing fields fail closed.

## Timeframe invariants

Restore validates:

- non-negative exact integer counters and offsets;
- lifecycle/gap/recovery consistency;
- contiguous, ordered retained bars with correct timeframe duration;
- finite ATR values and extrema;
- unique and ordered pivot/wave/hypothesis IDs;
- active-segment ordering and segment bounds;
- connected wave endpoint IDs and geometry;
- structure/hypothesis ownership by the active segment;
- bounded capacities;
- persistent Feature ID shape.

RC1 truncated waves that explicitly carry `path_truncated=true` may contain historical negative MAE/MFE caused by an earlier retained-path defect. Restore migrates only those two values to zero. The same invalid geometry without the migration marker is rejected.

Checkpoint serialization is JSON-compatible and deterministic. Restore is transactional: an invalid checkpoint does not replace the currently valid runtime state.
