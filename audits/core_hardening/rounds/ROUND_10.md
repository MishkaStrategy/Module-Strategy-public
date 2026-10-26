# Round 10 — checkpoint schema and durable persistence

- Checkpoint temporal/hash/state invariants are validated.
- Checkpoint manager verifies supported schema versions and uses constant-time checksum comparison.
- Atomic JSON persistence fsyncs the containing directory after replace.
