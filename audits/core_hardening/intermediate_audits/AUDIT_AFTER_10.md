# Intermediate audit after rounds 09–10

- Checkpoint schema: public version remains `1`; legacy checksum-free payloads still load.
- Module-state contract: unchanged; restoration is now prevalidated and rollback-protected.
- Persistence: atomic replace strengthened with directory fsync and size/integrity gates.
- Tests: full suite green.
- Restart/replay: materially improved; no production reconciliation claim made.
- Compatibility: existing v0.4 checkpoints remain readable.
- Regression risk: modules whose `restore_state` cannot restore their own `snapshot_state` are now exposed during rollback tests; this is a module contract violation, not a kernel API change.
- Decision: continue.
