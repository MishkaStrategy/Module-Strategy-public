# Logs Control Audit — owner-backed structured events required

Status: architecture audit for Issue #370. Unified Logs remains intentionally unavailable. Owner-contract follow-up: Issue #391.

## Decision

Do **not** implement a generic Control UI Logs surface by reading arbitrary files, running `tail`, accepting filesystem paths, executing shell commands, opening component databases directly, or inventing a new Control Plane event journal.

The live repository does not yet provide one authoritative, safe, multi-component operational event query that can truthfully back a unified Logs page.

## Existing evidence

### Generic Control Plane contract

`modular_control_plane.contracts` defines bounded common shapes such as `ControlEvent` and `EventType`.

Those common shapes are useful transport vocabulary, but the current Control Plane does not have an accepted producer/query implementation that owns cross-component event history. A transport model alone is not a durable event owner.

### Market Data

The native Market Data application owns typed durable job events and an SSE surface, including owner-specific event history/cursor semantics.

This is a valid owner source for Market Data, but it does not authorize the Control Plane to fabricate equivalent histories for other components.

### Backtest Control

The current Control Plane exposes durable Backtest job/result state. No accepted Backtest event-history query is exposed through the unified Control API.

Job status/result reads must not be reinterpreted as a complete log/event stream.

### Research / Strategy / Paper / Evidence / Signal Bot

The currently accepted Control UI slices expose bounded read projections for their specific owner state. The audit did not establish a common, no-path, structured operational event-query owner across these components.

Evidence is also not interchangeable with logs: immutable audit/results evidence has its own integrity and privacy semantics and must not be repurposed as a generic runtime log store.

## Rejected designs

The following designs are explicitly rejected for the current scope:

```text
GET /api/control/v1/logs?path=/some/file
GET /api/control/v1/logs?component=...&filename=...
tail -f ...
subprocess(...)
os.system(...)
read arbitrary cwd-relative files
open owner SQLite databases from the browser/Control Plane merely for logs
scan process stdout/stderr files by guessed paths
```

Reasons:

- path traversal and private host-path exposure;
- secret-bearing log lines, stack traces, environment or command-line leakage;
- duplicate ownership of retention/cursor semantics;
- read-side initialization/migration/locking risks for owner persistence;
- inability to prove completeness/order after restart;
- pressure to expose arbitrary shell/file capabilities that the Control Plane deliberately forbids.

## Required owner contract before enabling Logs

Issue #391 tracks the missing contract. At minimum, each intended owner source must provide or approve a bounded read-only query with:

- stable event identity and owner cursor;
- authoritative component/resource identity;
- timezone-aware UTC timestamp;
- bounded enum event type/severity/state;
- bounded, scalar-only sanitized payload;
- deterministic pagination/cursor semantics;
- explicit retention/truncation semantics;
- reader authorization;
- no filesystem paths;
- no environment variables/secrets;
- no argv/cmdline;
- no raw stack trace;
- no arbitrary owner payload/free-form secret-bearing text;
- no persistence migration, lease claim, recovery trigger or other read side effect.

Unknown/unsupported owner sources must fail closed rather than fabricate an empty or healthy stream.

## Aggregation rule

The Control Plane should remain a projection layer, not become a second durable event database.

Preferred design:

1. query authoritative owner event readers;
2. preserve each owner's stable cursor/identity;
3. sanitize into a common bounded transport contract;
4. merge only for presentation, with explicit source/cursor information and deterministic ordering rules;
5. do not persist a second copy unless a separate durability requirement and owner/recovery design is explicitly accepted.

If cross-owner total ordering cannot be proven, the UI must say so rather than imply one canonical sequence.

## Dashboard / terminal state

Until #391 is accepted:

- do not activate a unified Logs navigation item;
- do not expose `modular-control logs ...`;
- continue using existing component-specific read surfaces and Evidence pages for their intended purposes;
- surface actionable safe error IDs/codes where available, then inspect owner-side logs outside the Control UI through normal operational access.

## Acceptance tests for a future Logs slice

A future implementation must include focused tests for:

- reader authorization;
- bounded pagination/cursor validation;
- deterministic same-owner ordering;
- truncation/retention reporting;
- owner unavailable/unsupported behavior;
- rejection/redaction of path/env/argv/stack-trace/secret-bearing fields;
- no owner persistence mutation on read;
- restart behavior/cursor semantics;
- browser/CLI contract validation and AbortSignal/cancellation;
- no arbitrary filesystem or shell parameters in server/browser/client source.

Real owner-backed smoke is required for each enabled initial source. Mock-only aggregation is not sufficient acceptance.

## Current conclusion

The safe result of this audit is a deliberate unavailable Logs surface plus Issue #391. That is preferable to a feature-complete-looking log viewer that bypasses component ownership or leaks local-machine data.
