# Round 17 — isolated runtime-state restore

## Baseline

Checkpoint rollback retained the mutable mapping returned by `snapshot_state()`. A module that clears or mutates its state during restore could therefore destroy the backup used for rollback. Checkpoint collections and persisted scalar fields also relied on caller-provided sequence identity or implicit integer conversion.

## Change

- module-state and checkpoint collections are copied into validated tuples;
- duplicate module identities fail before restore;
- original rollback payloads are deep-frozen before any module is invoked;
- strict restore mode requires a real boolean;
- checkpoint manager rejects string-as-sequence schema configuration;
- persisted module rows, timestamps and cursor indexes are validated without coercion;
- checkpoint creation time cannot precede its runtime cursor.

## Result

A failed multi-module restore can roll back from isolated original snapshots, and malformed persisted state fails before runtime mutation.
