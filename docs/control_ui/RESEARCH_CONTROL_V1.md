# Research Control v1

## Purpose

Research Control v1 adds an operator-facing, read-only projection of existing Research Runner durable state to the local Modular Strategy Control Plane and Dashboard.

Research Runner remains the owner of experiment planning, scheduler execution, leases/fencing, retries, persistence, results, recovery and state transitions. Control Plane does not create a second scheduler and does not mutate Research state.

## Non-mutating persistence boundary

The regular `SqliteExperimentRepository` constructor can initialize or migrate the Research database. It is therefore not used by Control Plane for display reads.

Research Runner exposes an additive `ReadOnlyExperimentRepository` that:

- requires an already existing regular non-symlink SQLite file;
- opens SQLite with `mode=ro`;
- sets `PRAGMA query_only = ON` and `trusted_schema = OFF`;
- never calls the mutating repository initializer;
- never creates schema, migrates metadata, claims leases or changes run state;
- validates the current public repository schema version and required tables/columns, including fenced `run_claims` state;
- validates stored experiment/run status and job/result identities before projecting them;
- fails closed on missing, incompatible or corrupt durable state.

The read-only schema version is bound to `SqliteExperimentRepository.SCHEMA_VERSION`, rather than duplicated as a separate constant.

## Control Plane configuration

Set:

`MODULAR_CONTROL_RESEARCH_STATE_PATH=/absolute/path/to/existing/research.sqlite3`

The path must already exist, must be a regular file and must not be a symlink. `modular-control-api --check` reports only `research_control_enabled: true|false`; it does not print the configured filesystem path.

The Research adapter imports `modular_research_runner` lazily only when configured Research state is actually read. Importing the lightweight `modular_control_plane` package does not import Research Runner.

## API

All Research routes are reader-authenticated GET endpoints. There are no Research POST, PUT, PATCH or DELETE endpoints in v1.

- `GET /api/control/v1/research/experiments`
- `GET /api/control/v1/research/experiments/{experiment_id}`
- `GET /api/control/v1/research/experiments/{experiment_id}/runs`
- `GET /api/control/v1/research/runs/{run_id}`

Lists are bounded to at most 200 records per page. `has_more` is determined by an additional one-record read after a full page, including when the requested limit is 200; Control Plane never fabricates a false terminal page.

Typed errors include:

- `CONTROL_RESEARCH_NOT_CONFIGURED`
- `CONTROL_RESEARCH_NOT_FOUND`
- `CONTROL_RESEARCH_STATE_INVALID`
- `CONTROL_RESEARCH_UNAVAILABLE`

Unexpected exception messages are not returned. Only safe typed error identity and, for unexpected failures, exception type are projected.

## Public experiment projection

The public experiment contract contains only:

- experiment identity;
- specification SHA-256;
- native Research Runner experiment status;
- common Control Plane operator status;
- explicit `has_failures` partial-success flag;
- created/updated timestamps;
- run counts grouped by native run status.

`COMPLETED_WITH_FAILURES` remains native truth. It projects to common `SUCCEEDED` only together with `has_failures=true`; the UI therefore cannot mistake partial success for clean success.

## Public run projection

The public run contract contains only bounded operational identity and state:

- run/experiment IDs;
- native/common status;
- stage, split, dataset, scenario and stage-role identities;
- repetition, attempt/max-attempt and revision counters;
- created/updated/available timestamps;
- lease expiry for a currently RUNNING run, but no lease owner/session/token;
- failure category and retryability, but no raw failure message or exception detail;
- successful engine run identity, bars processed and restart-verification flag.

The following are intentionally not projected:

- job or result filesystem paths;
- raw parameters;
- arbitrary metadata or manifests;
- raw failure messages;
- worker IDs;
- session IDs;
- lease tokens;
- private engine diagnostics.

Public contracts independently validate native/common status mapping, lease/result consistency, attempt counters and partial-success semantics even though the native read-only port validates durable state first.

## Status mapping

Experiment projection:

| Native Research status | Common Control status |
| --- | --- |
| CREATED | QUEUED |
| VALIDATING | STARTING |
| PLANNED | QUEUED |
| RUNNING | RUNNING |
| PAUSED | PAUSED |
| CANCELED | CANCELLED |
| COMPLETED | SUCCEEDED |
| COMPLETED_WITH_FAILURES | SUCCEEDED + `has_failures=true` |
| FAILED | FAILED |
| BLOCKED | BLOCKED |
| CORRUPTED | FAILED |

Run projection:

| Native Research status | Common Control status |
| --- | --- |
| PLANNED | QUEUED |
| QUEUED | QUEUED |
| RUNNING | RUNNING |
| RETRY_WAIT | RECOVERING |
| SUCCEEDED | SUCCEEDED |
| FAILED | FAILED |
| CANCELED | CANCELLED |
| BLOCKED | BLOCKED |
| CORRUPTED | FAILED |

Native status is always retained alongside the common operator projection.

## Native Dashboard

`#/research` is a native workspace inside the single Dashboard shell and uses only the memory-held Control reader token.

It shows:

- Research component health;
- bounded experiment list;
- native/common experiment status and partial-success warning;
- run counts;
- bounded authoritative run list;
- safe run state/result identity and counters;
- failure category/retryability without raw failure detail.

It exposes no Start, Pause, Cancel, Retry, lease-management or scheduler controls. Those require a separate future idempotent/audited lifecycle contract owned by Research Runner.

## Browser acceptance

The focused exact-head Research E2E:

1. creates durable Research state through the real public fenced `SqliteExperimentRepository`;
2. creates one successful run with real result identity and one terminal failed run;
3. sets the experiment to `COMPLETED_WITH_FAILURES`;
4. records the Research SQLite SHA-256 and directory file list;
5. starts the real loopback Control API and Vite preview;
6. reads Research state through the native browser workspace;
7. verifies partial-success semantics and safe success/failure projections;
8. verifies private failure message, exception identity, worker/session/lease identity and filesystem paths are absent;
9. verifies there are no Research mutation buttons;
10. reloads the browser, proves the reader token is cleared, re-authenticates and sees the same durable server state;
11. verifies local/session browser storage remains empty;
12. verifies the Research SQLite SHA-256 and state-directory file list are unchanged after the entire browser flow;
13. verifies generated reader credentials do not appear in sanitized logs.

## Explicitly out of scope for v1

- starting experiments;
- pausing/canceling/retrying runs;
- lease claim/renew/release;
- scheduler ownership;
- schema migration;
- parameter or manifest editing;
- accepted-strategy mutation;
- live trading or private exchange capability.
