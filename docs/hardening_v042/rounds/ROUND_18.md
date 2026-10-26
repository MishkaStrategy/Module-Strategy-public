# Round 18 — bounded durable event persistence

## Baseline

The JSONL event store reopened logs with an unbounded whole-file `read_text`, followed symlinks, accepted booleans as operational integers, coerced persisted sequence strings with `int(...)` and had no total log-size ceiling or in-process append lock.

## Change

- reopen uses bounded binary line streaming;
- configurable total log and per-line ceilings;
- `lstat`, no-follow open, regular-file validation and inode comparison;
- strict `fsync`, line-size, log-size and cursor types;
- persisted sequence and timestamp fields are not implicitly coerced;
- append validation, durable write and memory publication are serialized by a lock;
- writes use `O_APPEND`, bounded size checks and optional `fsync`.

## Result

Event persistence is bounded and fail-closed against malformed rows, symlinks, path swaps, oversized logs and implicit type coercion while preserving deterministic append order.
