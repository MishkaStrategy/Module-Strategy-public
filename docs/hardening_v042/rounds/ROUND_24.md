# Round 24 — durable command-prefix consistency

Order submission, cancellation, protection updates, scheduled closes and cancel-all now commit mutable broker state only after the corresponding durable journal event is accepted. Multi-step cancellation leaves runtime state at the last durable prefix if a later event append fails.
