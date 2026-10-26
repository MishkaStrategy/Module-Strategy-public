# Round 16 — position ownership of reduce-only orders

## Defect
A reduce-only order could remain pending after the position was closed by a stop, target or scheduled close. It could block future entries or act on a later position.

## Fix
When a position lifecycle ends, every outstanding reduce-only order owned by that position is cancelled with structured reason `position_not_found` before further order processing.

No public API changed.
