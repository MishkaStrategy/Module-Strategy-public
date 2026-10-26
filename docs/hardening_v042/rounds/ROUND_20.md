# Round 20 — restart sequence and causation idempotency

Journal restart verifies the durable event stream, restores the next sequence and entity causation cursor, preserves legacy rows without entity keys, and rejects deterministic event-id collisions before append.
