# Local Core SQLite / Paper venue bridge

`modular_core.lifecycle_venue.SQLiteCorePaperVenue` is an opt-in adapter over
`SQLiteCoreLifecycleCommitPort`. It does not activate PAPER, construct a Runner,
qualify a Strategy Package, supply authority, or migrate an existing broker.
Existing runtime defaults and composition factories are unchanged.

The ledger remains the sole owner of broker state, lifecycle generation, fence,
canonical events, idempotency records and receipts. No mutable broker is exposed.
Submitted orders returned to Paper are detached projections; mutating one cannot
change committed state. Pending-order, position, event and Runner-compatible
evidence projections read the ledger. Evidence takes one SQLite snapshot so its
event cursor and position/order state describe the same commit.

## Durable context boundary

The host must implement `DurableCorePaperEffectSource.load_effect(effect_id)` over
an already persisted, immutable `CoreLifecycleEffect`. This source is a trusted
host dependency, not an authorization claim supplied by a caller. The bridge does
not implement the source because current Paper/Runner interfaces do not yet bind
their causal IDs and trusted authority sources to complete Core requests.

`venue.for_effect(saved_id)` loads the original request, preserving binding,
expected Core generation, writer epoch, authority references, targets and payload.
It returns a capability for exactly that operation and payload. Each invocation
rechecks canonical bytes before passing the saved request to the Core coordinator.
The bridge never derives an effect ID from a timestamp/client ID, increments an
in-memory ID, reads a new generation to rebase a retry, or falls back to a broker.

All unbound mutation methods reject. Missing sources, different payloads, stale
generations, missing authorities and closed-fence risk increases cannot become
successful venue returns. A non-COMMITTED result raises `CorePaperEffectNotCommitted`
with its original status, including UNRESOLVED. A successful invocation exposes
the matching durable Core receipt via `committed_receipt`. Retry/restart reloads
the same ID and lets Core replay the original receipt.

## Method coverage and the remaining composition gap

| Venue operation | Canonical Core effect | Local coverage |
| --- | --- | --- |
| `submit` (including reduce-only orders) | `submit_order` | temporary SQLite ledger and saved effect source |
| exact `cancel` (including the primitive used by OCO/cleanup) | `cancel_order` | same ledger; one exact target |
| `update_position` (primitive used by deferred protection/repair) | `update_position` | same ledger and exact position target |
| `schedule_close` | `schedule_close` | partial close and subsequent bar execution |
| `process_bar` | `process_bar` | same ledger; opt-in durable Runner caller and default-boundary regression |
| `cancel_all` | `cancel_all` | same ledger |
| direct priced `close_position` | unavailable | explicitly rejected; no raw venue fallback |

This establishes the method seam, not complete production call-site integration.
`PaperExecutionCoordinator` still calls venue methods without passing the saved
plan/intent/repair/OCO context. The existing effect contract offers scheduled
close semantics, not arbitrary caller-priced immediate close semantics.

## Opt-in durable Runner bar caller

`modular_trading_runner.core_lifecycle_bar.CoreLedgerPaperBarExecutor` is an
explicit alternative to the unchanged default `PaperBarExecutor`. Its trusted
`DurableCoreBarEffectLookup` must look up an already saved effect ID using the
exact original `(event_id, payload_hash, index)`. No production implementation of
that source is supplied. Missing or mismatched mappings fail before a pending
attempt or broker mutation is saved.

Before invoking Core, the caller durably stores the returned effect ID and full
Core request digest with the original market event and index. It uses a separate
state key and rejects any existing legacy bar state for that run until an owner
migration exists; it cannot silently start beside the old cursor. Failed/ambiguous
Core outcomes retain that frozen reference. Restart loads the same effect ID,
verifies the full request digest and canonical bar invocation, then uses Core's
idempotent receipt to resolve the pending bar. It does not perform another
event-to-effect lookup, rebase generation, or infer success from changed venue
evidence. A changed saved request fails closed even if it is structurally valid.

Synthetic tests exercise absent/wrong-index mapping, ordinary duplicate replay,
lost acknowledgement after durable Core COMMIT and process restart, stale Core
generation, and an attempted source rebase. The actual existing Paper coordinator
is also tested with absent context and with a committed Core effect whose
acknowledgement is lost.

## Paper post-COMMIT handling: remain nonterminal; do not activate

`coordinator.py:accept_plan` now intercepts only the explicit bridge exception
`CorePaperEffectNotCommitted` with status `UNRESOLVED` before its generic venue
failure handlers. It retains the existing write-ahead `execution.intent_recorded`,
blocks the scope and recovery lifecycle, and raises `ReconciliationBlocked`.
It does not compensate/cancel, append `execution.failed`, or cache a terminal
result for that ambiguous Core attempt. Known denied Core outcomes and normal
non-Core venue failures retain their existing behavior.

The temporary durable-journal regression demonstrates that the Core order may
already be committed while Paper's intent remains open. Immediate retry and a
reconstructed coordinator both refuse dispatch; reopening the journal does not
recast that intent as failure or acceptance. This hardening preserves uncertainty
and blocks the scope. It does not supply a receipt-driven resolution policy.

The opt-in venue also raises `CorePaperCommittedProjectionUnavailable` with its
immutable invocation reference and Core receipt when the durable commit succeeded
but a subsequent order projection read/decode fails. Before submit/cancel, the
Paper coordinator captures a `CoreInvocationRef` from the already saved request;
after a successful call it accepts only the exact receipt for that reference.
Legacy `committed_receipt` fields alone do not establish the current invocation.
Receipt-aware wrappers must declare `core_receipt_aware = True` and forward both
`invocation_ref()` and `receipt_for_invocation()`; a marked wrapper missing either
method is rejected before mutation.
Any wrapper that delegates Core mutations but omits that declaration is an
unsupported composition and cannot be qualified; the runtime cannot infer hidden
delegation from an unrelated stale receipt field. A future production composition
factory must require the receipt-aware contract across every Core-backed path.

Once the exact Core mutation is known committed, any following Paper validation,
projection, journal append/fsync-ack or plan-publication failure blocks the scope
and raises `ReconciliationBlocked`. It does not compensate/cancel, write
`execution.failed`, or cache a terminal plan result. Tests cover projection
failure and `order.submitted`/`plan.accepted` append faults both before write and
after durable write with a lost acknowledgement. Deferred-protection publication
failure also clears the accepted plan cache and hash; retries and reconstructed
services remain blocked. These guards preserve the write-ahead facts without
proving full Paper/Core correspondence or resolving the attempt.

Broader Paper caller wiring still needs durable per-intent Core attempt
references and explicit receipt-driven reconciliation before
compensation, and frozen attempt context for OCO/protection/repair retries. Those
paths currently reconstruct timestamps and fresh marks. Replacing their saved
request with those new values would violate idempotency; ignoring freshness
checks would change safety semantics. No such policy is invented here.

`prepare_local_core_paper_binding(...)` therefore always returns an explicitly
unqualified `LocalCorePaperLedgerBinding`. `require_runtime_qualified()` rejects
even if a source was supplied. A source's existence is not proof that every
production caller binds the right durable context. No production composition may
treat this local binding as qualification.

The next implementation must bind the remaining Paper causal call sites to
persisted requests, handle each Core receipt/ambiguous outcome in Paper's durable
reconciliation, and connect Lifecycle to this exact ledger. Trusted
instance/package/writer/authority/deployment bindings and evidence for migrating
any populated owner must come from their actual owners. Missing inputs remain
blocked. Cross-store Paper journals and Core state are not one SQLite transaction.

No SEARCH or PAPER workload, external owner data, deployment, or CI execution is
required or authorized by this local bridge.

## Evidence-only pending-attempt inspector

`paper_execution.core_attempt_evidence` provides an opt-in, read-only inspector
for durable Paper-to-Core attempt links. Its `CORE_RECEIPT_MATCHED` status means
only that the saved Core request has a matching historical Core receipt. The
link currently checks plan ID, plan content hash, intent ID, and client-order ID.
`intent_correspondence` is always `UNVERIFIED`: this seam does not establish that
the original plan and Core request correspond in side, quantity, prices,
normalization, metadata, mark, time, or index. It therefore does not establish
Paper plan acceptance or unblock any attempt. Terminal Paper outcomes that
contradict Core receipt evidence are reported as `CONFLICT`; unresolved legacy
intents without links remain blocked.

The Core receipt lookup compares the immutable instance/incarnation/package
binding before returning `ABSENT_AT_READ`; a foreign aggregate is `CONFLICT`.
For a same-scope absence it reports the validated snapshot's commit and event
sequences and does not require the request's historical writer epoch to equal the
current writer.

The synchronous coordinator also captures an in-memory `CoreInvocationRef` from
an exact bound request before Core submit/cancel and accepts only a receipt that
matches that reference and the same saved request. A stale receipt exposed by a
legacy wrapper is not evidence for the current invocation. A Core-bound adapter
wrapper must declare `core_receipt_aware = True` and forward both
`invocation_ref()` and `receipt_for_invocation()`; a marked wrapper missing either
method is rejected before mutation. This reference is not persisted per Paper
intent; if the process loses its Paper acknowledgement, the durable intent remains
blocked on restart pending owner reconciliation. This does not wire the bridge
into production or establish full plan correspondence.
