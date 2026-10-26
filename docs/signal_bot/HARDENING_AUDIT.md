# Signal Bot hardening audit

Scope: PR 52, branch `feature/modular-signal-bot`. The PR remains Draft and no merge, trading,
release, tag, package publication, force push or real Telegram/Binance operation is authorized.

## Three completeness reviews

1. Architecture: removed arbitrary YAML imports and fake portfolio state; added a deterministic
   strategy registry, fail-closed module capabilities, and a Core event-consumer path.
2. Security and recovery: enforced command allowlists and the single-recipient model, persisted
   Telegram offsets, serialized SQLite access, UTC writes, HTTPS-only adapters and bounded text.
3. Operations and evidence: implemented chart-free delivery, expanded failure/restart/concurrency
   tests, added Python 3.12/3.13 fast gates and an exact-SHA manual heavy workflow.

## Ten substantive optimization/security rounds

| Round | Finding | Improvement and regression evidence |
|---:|---|---|
| 1 | Any chat could query state | Fail-closed allowlist; unauthorized-update tests |
| 2 | A secondary chat could disable primary delivery | Explicit primary preference ownership |
| 3 | Polling offset was memory-only | Atomic monotonic SQLite offset; restart/replay test |
| 4 | One connection had incomplete read locking | One re-entrant lock and busy timeout; threaded stress test |
| 5 | Chart flag did not control the pipeline | Optional chart protocol and renderer-not-called test |
| 6 | YAML could import installed Python | Registered IDs and hostile/duplicate/version tests |
| 7 | Adapter fabricated portfolio values | Explicit capabilities and rejection tests |
| 8 | Validation admitted gaps/stale decisions | Continuity, invalidation and staleness tests |
| 9 | Integrated delivery risked divergent identity | Core ingest same-ID/dedup test |
| 10 | Operational proof was incomplete | SHA-pinned soak, security, SBOM and wheel workflow |

Even rounds additionally reviewed architecture, security, idempotency, SQLite, Telegram access,
memory and operational reliability. The final audit found no order execution path and no claim of
exactly-once delivery. GitHub runner results remain required before integration readiness.
