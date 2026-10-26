# Independent Strategy Audit & Validation

This package implements the separate `strategy-audit-port-v1` authority required
before a Strategy Factory candidate may be admitted to **paper runtime**.

It verifies a complete immutable audit package, including Dataset Registry
lineage, chronological research windows, candidate/configuration identity,
Results Aggregator evidence, robustness, one-time holdout isolation, execution
assumptions and risk policy. Decisions are content-addressed, HMAC-bound, stored
in SQLite with a tamper-evident append-only chain and replayed idempotently.

The component has no live authorization enum, method, credential path, exchange
client, order path or deployment behavior. An approved decision can only be
`APPROVED_FOR_PAPER`.

`IndependentStrategyAuditor.verify_stored_decision(expected=...)` checks an
already stored decision against the complete durable chain, every historical
decision HMAC, and a signer-authenticated binding between the requested decision,
its full canonical request hash, and the policy fingerprint used when it was
issued. It then requires exact equality with the expected immutable decision.
It reads through the auditor's existing store connection and creates no decision
or store state. This is a method on an already configured auditor;
constructing `SQLiteAuditStore` is not read-only because construction creates
directories/database state and configures WAL. A valid signature establishes
authenticity only: it does not turn a `REJECTED`, `RESEARCH_ONLY`, or
`UNAVAILABLE` outcome into approval. Missing verification authority, unavailable
storage, a mismatched identity, a missing request binding, or tampering fails
closed. Schema-v1 decisions remain readable and replayable through the legacy
API, but are explicitly unbound and cannot pass this stronger verification
method. The v1-to-v2 migration does not infer or backfill bindings. The HMAC key
remains inside the auditor; Factory decision payloads and codecs are unchanged.

## Audit-store schema upgrade and rollback

Opening an existing writable v1 audit store with this package upgrades the
schema transactionally to v2. Existing rows remain available to legacy read,
query, and replay APIs, but receive no inferred request binding and cannot pass
`verify_stored_decision`. If migration fails, the transaction rolls back and
the store remains v1. A v2 store with a missing binding table is treated as
corrupt; the package does not recreate the table or backfill it. Binaries that
only understand v1 metadata must not open a v2 store and will fail closed.

To roll back the application version after migration, stop audit writers and
restore the complete pre-migration database snapshot using the owning service's
verified backup-and-restore procedure, then verify the restored v1 store before
starting the older binary. Do not downgrade only `schema_version`, drop the
binding table, or copy individual SQLite files while the database is active;
those operations can discard signed bindings or break the journal state. If a
verified pre-migration snapshot is unavailable, keep the compatible v2 binary
and recover from a verified current-format backup instead.
