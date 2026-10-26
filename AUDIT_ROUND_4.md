# Audit after optimization round 4

## Scope

Review of hot-path allocations, feature visibility, event-store reads, durable event persistence, immutable event payloads and replay integrity after rounds 3–4.

## Validation performed

- Full pytest suite: **73 passed**.
- Complete engine-generated event stream passed causation, sequence, run-id and timestamp verification.
- JSONL event store closed/reopened successfully with preserved immutable payloads.
- Atomic JSON state write/read/delete tested.
- Micro-benchmark: 20,000 bars × 10 no-op modules ≈ **2.29 seconds** in the current container.

## Findings fixed

1. **Repeated feature materialization — medium performance impact.** Multiple modules reading unchanged features on the same closed bar caused repeated grouping and sorting. Feature snapshots are now revision-cached.
2. **Linear event-tail reads — low/medium performance impact.** Durable/in-memory consumers could scan the whole event list for a tail. Sequence boundaries now use binary search.
3. **Shallow event immutability — high audit risk.** Nested payload data could be mutated after append. Event payloads and feature payloads now share deep-freeze semantics.
4. **No durable event implementation — high operational risk.** Added append-only JSONL storage with validation during reopen and optional fsync.
5. **No replay integrity check — high audit risk.** Added verification for duplicate IDs, mixed runs, non-increasing sequences, timestamp regression and future/unknown causation.
6. **Unsafe state writes — medium operational risk.** Added an atomic JSON repository based on fsync + `os.replace`.

## Remaining risks for later rounds

- Strategy module internal state still lacks a standard checkpoint contract.
- Engine cannot resume a partially processed historical run from a checkpoint.
- Config loader accepts some Python coercion ambiguities and needs a frozen resolved configuration artifact.
- Package lacks CI and compatibility matrix.
- Event verification checks structural integrity, not cryptographic tamper evidence.

## Decision

Rounds 3–4 are accepted. Persistence and replay primitives are usable; live restart support still requires module and kernel checkpoints in the next round.
