# Round 03 — event commit semantics and replay continuity

- Journal sequence numbers are now committed only after the event store accepts the event.
- A persistence failure can be retried without creating a hidden sequence gap.
- Replay verification now reports missing sequence numbers, not only non-increasing order.
