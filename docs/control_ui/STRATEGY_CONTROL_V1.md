# Strategy Control v1

## Scope

Strategy Control v1 is a read-only Control Plane projection of the existing Strategy Factory
governance state. Strategy Factory remains the authority for checkpoints, decision logs, stages and
native run status. The Control Plane does not create a second scheduler or state machine.

The first v1 slice intentionally exposes no Strategy mutation endpoint. It is designed to make
existing governance state visible before any create/resume/cancel workflow is admitted to the
shared operator surface.

## Server-owned state root

Strategy Control is enabled only when `MODULAR_CONTROL_STRATEGY_FACTORY_STATE_ROOT` points to an
existing ordinary directory. The configured root is server-owned configuration and is never
returned to the browser.

The reader accepts only bounded run identities matching the existing safe Control identity format.
It does not accept arbitrary filesystem paths from API or browser clients. Run directories and
checkpoint directories must already exist and must not be symlinks before Strategy Factory readers
are constructed.

This precondition matters because the native checkpoint and decision-log constructors create their
parent directories with `exist_ok=True`. By validating existing directories first, the Control Plane
keeps the read path observational: reading an absent run must not create Strategy Factory state.

## Integrity model

For every projected run the adapter delegates integrity work to Strategy Factory primitives:

- `AtomicCheckpointStore.load()` validates the durable checkpoint wrapper and payload checksum;
- `DecisionLog.read_all()` validates every decision entry and the tamper-evident hash chain;
- every decision must carry the requested run identity;
- the checkpoint `decision_chain_head` must equal the actual final decision entry hash, or both must
  be empty.

A run that fails these checks is not projected as trustworthy state. A bounded list scan records the
invalid entry count and component health becomes `DEGRADED`. Direct access to an integrity-failed
run returns a typed Control error rather than raw exception text.

## Public projection

The browser receives bounded governance identity only:

- `run_id`;
- native Strategy Factory status plus the common Control `JobStatus` projection;
- native stage and monotonic stage sequence;
- checkpoint `updated_at`;
- request and dataset fingerprints;
- source commit SHA and manifest identity;
- counts of completed artifacts, submitted batches, processed events and decisions;
- decision-chain head and verification flag.

Filesystem paths, raw checkpoint JSON, raw artifact payloads, decision contents and exception bodies
are not part of the v1 API contract.

## Status projection

Native Strategy Factory state remains authoritative. The common status exists only for cross-module
operator UX:

- `CREATED` -> `QUEUED`;
- `RUNNING` -> `RUNNING`;
- `PAUSED` -> `PAUSED`;
- `CANCELED` -> `CANCELLED`;
- `COMPLETED` -> `SUCCEEDED`;
- `FAILED`, `DATASET_REJECTED`, `CORRUPTED` -> `FAILED`;
- `BUDGET_EXHAUSTED`, `BLOCKED` -> `BLOCKED`.

The native status is returned alongside the projection so the Dashboard never loses Strategy
Factory semantics.

## API

Reader-authenticated surfaces:

- `GET /api/control/v1/strategy/runs?limit=...&offset=...`
- `GET /api/control/v1/strategy/runs/{run_id}`
- `GET /api/control/v1/components/strategy/health`

There is no Strategy mutation endpoint in v1.

Typed failure modes include:

- `CONTROL_STRATEGY_NOT_CONFIGURED` when no state root is configured;
- `CONTROL_STRATEGY_RUN_NOT_FOUND` for an unknown safe run identity;
- `CONTROL_STRATEGY_RUN_INTEGRITY_FAILED` when checkpoint/decision integrity cannot be trusted;
- generic unexpected failures are reduced to safe Control Plane error type metadata.

## Native Dashboard workspace

`#/strategies` is a native page inside the single Control Dashboard shell. It uses the existing
memory-only Control reader token and does not require the operator token.

The page shows:

- Strategy component health and bounded scan warnings;
- existing Factory runs ordered by authoritative checkpoint update time;
- native status, common status and current stage;
- fingerprints, source commit, manifest identity and integrity counters for the selected run;
- whether the decision chain is verified.

It deliberately has no Edit, Resume, Cancel or "make accepted" control. It exposes no raw path or
filesystem picker.

## Accepted-strategy immutability

The accepted/canonical strategy is never edited in place through Control UI. A future mutation
surface must create a new candidate/config identity and pass the Strategy Factory governance and
acceptance workflow before it can become canonical. This v1 slice only observes existing identities.

## Research boundary

Research Runner remains a separate durable owner. Its current public surface can query an explicit
experiment identity, but the audited public API does not yet provide a safe read-only experiment
listing/discovery port. Strategy Control does not bypass that boundary by opening/migrating the
Research repository or by implementing a second scheduler.

Research Dashboard integration remains pending until a backwards-compatible ownership-safe query
surface exists.

## Acceptance requirements

Before this slice is accepted:

1. Python 3.12 and 3.13 exact-head Control Plane gates must pass with Strategy Factory installed while
   the base `modular_control_plane` import still proves `strategy_factory` is not eagerly imported.
2. Reader tests must prove existing checkpoint state is read without creating new run state,
   checksum/hash-chain failures fail closed, and symlinked run roots are never followed.
3. Strict TypeScript and unit tests must validate the browser contracts and safe reader client.
4. A real-loopback browser test must read a checkpoint written by the actual Strategy Factory store,
   show it in the native Strategies workspace, clear the reader token on reload, then show the same
   server-owned run again after re-authentication.
5. The browser must expose no Strategy mutation control, filesystem path or persistent credential.
6. Existing Backtest and Market Data acceptance must remain green.
