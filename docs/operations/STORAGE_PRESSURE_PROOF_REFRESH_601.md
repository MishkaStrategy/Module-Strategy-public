# GAP-013 storage-pressure proof refresh

Issue: #601  
Parent: #540 / #390 / #373  
Base: `integration/modular-strategy-full-rc@73374bbaa6cd109696e48862bb2c9b83840e444e`

This checkpoint records why `STORAGE_PRESSURE_THRESHOLDS.json` no longer marks already accepted owner proofs as pending.

- System Acceptance: #590 / PR #591 accepted bounded real `SQLITE_FULL` rollback/recovery and integrity proof. Its existing 1 GiB database bound remains an owner DB bound; no host free-space high/critical watermark is inferred.
- Research: #592 / PR #593 accepted bounded SQLite `SQLITE_FULL` atomicity/retry/reopen for scheduler and Results Registry. #562 / PR #596 accepted atomic artifact `ENOSPC` failure/retry cleanup. Existing Wave 2A size bounds remain measurement/regression bounds, not host free-space thresholds.
- Market Data: #565 established representative publication byte scale; #595 / PR #600 accepted dynamic next-publication admission for CSV/Parquet before the first immutable version write. It uses serialization-parity partition sizing, exact prospective manifest bytes, allocation-unit rounding and a 24,576-byte registry transaction/recovery reserve. This is operation-specific admission, not a global percentage watermark.
- Core/Paper/Trading: current full-RC exposes durable-capable Core `EventStore`/`StateRepository` implementations, but Paper and Trading consume injected contracts and their current owner tests use in-memory backends. No concrete persistent paper-runtime backend is selected in current full-RC, so physical capacity remains `UNKNOWN_DO_NOT_PRUNE` rather than receiving a fabricated disk threshold.

No deletion authority, retention rule, runtime behavior, strategy/dataset semantics, deployment capability or Live path is introduced by this refresh.
