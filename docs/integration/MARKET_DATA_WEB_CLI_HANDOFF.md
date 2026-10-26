# Market Data CLI, API and Web Console — implementation handoff

- Updated: 2026-07-29
- Canonical repository: `MishkaStrategy/Module-Strategy`
- Main Draft PR: #223
- Interface branch: `interface/market-data-web-cli`
- Current accepted product merge head: `1b8852d125b20dc3871dc587cc6b5feafbb525ee`
- Backend dependency branch: `data/historical-market-data-ingestion`
- Backend Draft PR: #221
- Backend synchronized head: `69647bdb13d002dcd5768fdca9405af3ce3f4b05`
- Integration target after full acceptance: `integration/modular-strategy-v0.6.0-rc`

## Safety boundary

This remains a Draft development track. Do not merge to integration, staging or `main`; do not
squash, rebase or force-push; do not publish packages, create releases, deploy, enable external bind,
add private exchange credentials, mutate production datasets or perform live trading.

Tests use `FakeExchangeAdapter`, temporary local stores, generated local bearer values and loopback
processes only.

## Accepted stacked history

- backend synchronization PR #253 — merge `a34bd70aca2c546990ca565d6390e97aaf5d504c`;
- export/artifact PR #251 — merge `5f7c18d2f54859ec75f406627548cf61afadefd2`;
- Web Console foundation PR #254 — merge `740177774640a7e78bdfac347031caacba4d1957`;
- job observability PR #255 — merge `345c96e162bacbfcbe138afce5becc1296f40d16`;
- real browser E2E PR #256 — merge `1d35894a904da477b84de171a9f1f44a052d80a0`;
- legacy CLI parity audit PR #257 — merge `66f9d7248616346d4c00495c5b64350692de8fb6`;
- parity parent-boundary CI fix PR #258 — merge `f6ddcf1a16c5a7258fcdfcec5bfe74a5b2da5ce6`;
- read-only exchange parity PR #259 — merge `54e89e203be62e800e3058b74e59c5b4c9f5405a`;
- durable UPDATE PR #260 — merge `1f7419a582a93c80dc46012f953ecff8af82e751`;
- durable BACKFILL PR #261 — merge `584db883a8010a19bc45dc0b44dbbcc4fce608a7`;
- safe legacy download migration PR #262 — merge `179f85b9900e979a0ffdc504d39253bd6ee19388`;
- measured durable download progress PR #263 — merge `1b8852d125b20dc3871dc587cc6b5feafbb525ee`.

All accepted stacked PRs used normal merge commits into `interface/market-data-web-cli`. No integration,
staging or `main` merge, release or deployment was performed.

## Canonical architecture

`market_data_ingestion` remains authoritative for exchange acquisition, planning, checkpointing,
normalization, validation, repair, aggregation, hashing, immutable storage and Dataset Registry
publication.

`market_data_application` provides the transport-neutral durable control plane:

- strict Pydantic v2 transport and persisted-job contracts;
- canonical request hashing and backend-owned execution identity;
- one validated settings and composition root;
- durable SQLite jobs, actor-scoped idempotency and append-only events;
- paginated job queries and bounded event replay;
- cross-process leases, heartbeat, fencing and stale takeover;
- startup recovery and post-publication reconciliation;
- reader/operator/admin bearer roles;
- deterministic application-owned artifacts;
- no duplicate exchange, validation, repair, update, backfill, hashing or storage state machine.

Backend `RunState`, immutable manifests and Dataset Registry descriptors remain authoritative. Process
threads, progress observers and HTTP transports are non-authoritative adapters around those contracts.

## CLI and migration entry points

The legacy `market-data` entry point remains installed and unchanged.

`market-data-v1` emits structured JSON and includes:

- `version`, `doctor`, `exchanges`;
- `capabilities --exchange <exchange-id>`;
- `markets --exchange <exchange-id>`;
- `instruments --exchange <exchange-id> --market <market-type> --symbol <symbol>`;
- `download --request <json> --idempotency-key <key>`;
- `datasets list/show/validate/repair-preview`;
- `datasets repair-confirm/repair-confirmation-show`;
- `datasets repair-execute --request <json> --idempotency-key <key> --actor-id <actor>`;
- `datasets update <dataset-id> --request <json> --idempotency-key <key> --actor-id <actor>`;
- `datasets backfill <dataset-id> --request <json> --idempotency-key <key> --actor-id <actor>`;
- `datasets export <dataset-id> --idempotency-key <key> --actor-id <actor>`;
- `artifacts show <artifact-id>`;
- `jobs list/show/events/cancel`.

The isolated base-dependency migration tool is:

```text
market-data-v1-migrate --config legacy.yaml [--as-of ISO-8601] [--output request.json]
```

The legacy, v1 and migration entry points are checked side by side from clean wheels.

## HTTP API and local Web Console

FastAPI `/api/v1` provides public version plus protected health, exchange discovery, immutable Dataset
Registry inspection, validation, repair preview/confirmation/execution, durable DOWNLOAD/UPDATE/
BACKFILL/EXPORT submission, artifact delivery, job inspection/cancellation and event replay/SSE.

Reader/operator authorization, OpenAPI and structured errors are shared across routes. External bind
remains prohibited; accepted server hosts are loopback only.

The React 19 / TypeScript / Vite / TanStack Query console is read-only. Bearer values remain in React
memory and do not enter browser storage, source control or query keys. Playwright uses real loopback
FastAPI and Vite processes, fake immutable datasets, a generated masked reader token and isolated
Chromium.

## Accepted durable mutation semantics

### Repair

The accepted execution action is `stream_redownload_and_rebuild`. Confirmation is bound to the exact
dataset/version/preview/action/stream/actor, has bounded TTL, persists only a token digest and is
single-use. Unsupported actions and active-repair cancellation remain fail-closed.

### UPDATE

UPDATE pins the exact immutable parent version/hash/end, validates selected existing streams, scopes
visible idempotency to the actor, uses leases/fencing and calls backend `DatasetOperations.update` with
deterministic `request_id=job_id`. Recovery reconciles the same child without another version.

### BACKFILL

BACKFILL accepts exactly one implemented historical series and an explicit aware UTC range. It pins
the exact parent version/hash and normalized stream timeframe. Same-stream overlap returns
`BACKFILL_RANGE_OVERLAP` with `use_repair=true`; adjacent ranges and missing supported streams are
allowed. Recovery reconciles the same child without another version.

### Export

EXPORT pins an immutable Dataset Registry version, writes only below the application artifact root,
produces deterministic ZIP evidence, verifies manifest identity and partition checksums, rejects
traversal/symlinks and atomically recovers ZIP/sidecar halves without duplicate artifacts.

## Safe legacy download YAML migration

Migration delegates schema and semantic validation to backend `load_config`, then emits one strict
deterministic `CreateDownloadRequest` JSON document without acquisition or Registry mutation.

It preserves representable exchange, market, instrument, streams, timeframes, UTC range, storage
format/compression, gap policy, concurrency and memory bound. Dynamic `latest_closed` requires
explicit `--as-of`.

Explicit legacy request identity, arbitrary non-default storage roots, `resume: false`, request-external
runtime/validation controls and credential-like fields fail closed. Optional output is atomic, private,
no-clobber and refuses symlink/non-regular paths.

## Measured durable download progress

Direct durable `DOWNLOAD` jobs now reuse existing backend evidence rather than estimating work:

1. exact ordered `DownloadPlan.chunks`;
2. validated checkpoint-completed chunk IDs;
3. committed pipeline chunk outcomes emitted in deterministic plan order.

A ContextVar-scoped backend observer is recursion-safe and non-authoritative. Observer or progress
persistence failure cannot change ingestion results.

The canonical application maps snapshots into the existing durable `JobProgress` contract and fenced
`SQLiteJobStore.record_progress` writes:

- `PLANNING` and non-download phases remain indeterminate;
- `DOWNLOADING` is determinate only when an exact historical chunk total exists;
- completed units are unique checkpoint-completed plus newly committed chunks;
- counters include planned/checkpoint/downloaded/restored chunks, records and downloaded raw bytes;
- the current committed UTC range is exposed without source URI, raw payload or credentials;
- same-phase totals, completed units and counters cannot regress;
- resumed processes initialize from the validated checkpoint and last durable snapshot;
- worker completion order is not public progress order;
- REST, SSE, CLI and Web consume the same stored snapshots/events.

No synthetic whole-job percentage or cross-phase weighting was introduced.

## Legacy CLI parity audit

The machine-readable audit enforces actual legacy argparse, v1 Typer/Click and migration argparse
surfaces, positional identifiers, one classification per legacy command and AST-derived exit codes.

Current classifications:

- exchange reads, inspect and validate: equivalent;
- download, status, repair, UPDATE, BACKFILL and cancellation: safely replaced;
- caller-selected dataset export `--target`: intentionally incompatible;
- manual backend run-ID resume: intentionally incompatible.

Observed exit-code sets:

- legacy: `0,1,2,3,4,5,6,7,8,9,10,11`;
- v1: `0,1,2,3,4,6,7,9`;
- migration: `0,1,2,7,9`.

## Exact-head CI evidence

### Measured-progress feature head `1f366aeff72b09e844ac75eda5a4f730e9a85a69`

- measured progress `30449871339`: Python 3.12/3.13 green;
- durable BACKFILL `30449871335`: Python 3.12/3.13 green;
- package `30449871329`: Python 3.12/3.13 green;
- export `30449871334`: Python 3.12/3.13 green;
- core `30449871358`: Python 3.12/3.13 green.

### Accepted measured-progress merge head `1b8852d125b20dc3871dc587cc6b5feafbb525ee`

- migration `30450707987`: Python 3.12/3.13 green;
- core `30450707942`: Python 3.12/3.13 green;
- durable BACKFILL `30450708051`: Python 3.12/3.13 green;
- export `30450708070`: Python 3.12/3.13 green;
- browser E2E `30450708012`: green;
- exchange read parity `30450708143`: Python 3.12/3.13 green;
- measured progress `30450708131`: Python 3.12/3.13 green;
- CLI parity `30450708086`: Python 3.12/3.13 green;
- package/interface `30450708133`: all Python 3.12/3.13 jobs green;
- Web Console `30450708140`: green;
- durable UPDATE `30450708053`: Python 3.12/3.13 green.

These workflows cover compile, Ruff, strict MyPy, static audits, focused and full tests/coverage,
distributions, Twine, clean-wheel entry points, migration, progress resume/concurrency/failure isolation,
UPDATE/BACKFILL recovery, parity, export integrity, Node security gates, production Web build, real
browser E2E and Core integration.

## Next exact slice — authenticated local control-plane hardening

Create a separate stacked Draft from the final exact-green documented interface head. External bind
must remain prohibited.

Current audited gaps:

- bearer tokens are a static plaintext-keyed environment JSON map transformed to in-memory digests;
- token configuration has no generation, overlap window, revocation timestamp or atomic reload;
- absence of an authenticator gives loopback requests an implicit admin principal;
- no per-principal/direct-peer request-rate limiter exists;
- no explicit request-body/header/event-stream admission limits are composed at the API boundary;
- no durable authenticated administrative audit log exists;
- proxy headers are not trusted and must remain ignored in this local slice.

Required implementation boundary:

- retain loopback-only bind validation and ignore forwarded-client headers;
- add bounded request body, header and concurrent SSE admission before route execution;
- add deterministic per-principal plus direct-peer token-bucket limits with bounded memory and stable
  `429`/`Retry-After` responses;
- exempt only public version and narrowly defined health behavior; mutation routes must never bypass
  authenticated limits;
- introduce immutable token records identified by non-secret token IDs and SHA-256 digests only;
- support atomic configuration reload with an explicit generation and bounded old/new overlap window;
- allow immediate revocation and reject rollback/reuse of stale generations;
- never return, persist or log plaintext bearer values;
- require explicit authentication for administrative rotation/audit operations; no implicit admin on
  those operations;
- append durable tamper-evident administrative audit records for authentication failures, authorization
  denials, token generation changes, revocations and rate-limit decisions;
- bind audit records to correlation ID, direct peer, principal/token ID, action, outcome and UTC time;
- define retention/size bounds and fail closed for admin mutations when audit durability is unavailable;
- keep normal read-only and accepted mutation semantics unchanged;
- prove concurrency, restart, overlap expiry, revocation, rollback rejection, rate-limit recovery,
  request-size rejection, audit-chain verification and secret non-disclosure;
- add Python 3.12/3.13 focused CI plus all inherited Market Data profiles.

Do not add TLS, trusted proxies or external bind in this slice; those remain a separate later gate.

## Remaining release gates

- accepted authenticated local control-plane hardening;
- additional repair actions only after action-specific recovery proof;
- TLS/trusted-proxy policy before external bind;
- process-crash/lease-expiry, pagination, artifact and SSE stress tests;
- performance/memory and dependency/server security evidence;
- 30 substantive hardening rounds;
- final integration and release review.

## Completion status

- backend ingestion acceptance: passed; integration merge not authorized;
- backend-to-interface synchronization: complete and verified;
- durable application, CLI v1 and loopback FastAPI v1: implemented for accepted scope;
- exchange read parity: implemented and verified;
- repair confirmation/execution: implemented for `stream_redownload_and_rebuild` and verified;
- durable UPDATE: implemented, merged and exact-head verified;
- durable BACKFILL: implemented, merged and exact-head verified;
- safe legacy download migration: implemented, merged and exact-head verified;
- measured progress counters: implemented, merged and exact-head verified;
- durable export/artifacts: implemented and verified;
- Web Console and real browser E2E: implemented and verified;
- legacy parity audit: implemented and enforced;
- authenticated local control-plane hardening: not implemented;
- release gate: not passed;
- PR #223 must remain Draft.
