# ADR 0005: Authenticated Local Market Data Control Plane

- Status: accepted for implementation
- Scope: local loopback Market Data API only
- Release mode: paper-only

## Context

The accepted Market Data API and Web Console bind only to loopback and support
reader, operator and administrator roles. The first interface slice intentionally
used a static in-memory bearer-token map. It did not provide bounded admission,
rate recovery, durable token generations, atomic reload, revocation, rollback
protection or a tamper-evident administrative audit.

This ADR defines the hardening boundary without adding TLS, trusted-proxy logic,
external bind, private exchange credentials or any live-trading capability.

## Decision

### Network and peer identity

- API bind remains restricted to `127.0.0.1`, `localhost` and `::1`.
- The direct socket peer is authoritative. Forwarded headers are ignored.
- Correlation IDs are bounded, validated and returned to the caller.

### Bounded admission

The control plane rejects requests before application mutation when any configured
bound is exceeded:

- aggregate request-header bytes;
- declared or observed request-body bytes;
- concurrent SSE sessions globally, per principal and per direct peer;
- bounded-memory rate-limit state.

Rate limits use deterministic token buckets for both principal and direct peer.
Rejections return stable `429` responses with `Retry-After`. Bucket state is capped
and evicted deterministically so untrusted identifiers cannot grow memory without
bound.

### Token state

Persistent token records contain only:

- a non-secret token ID;
- a SHA-256 digest;
- role;
- explicit token-set generation;
- activation and optional expiry timestamps;
- revocation state.

Plaintext bearer tokens are never written to SQLite, audit records, responses or
logs. Token snapshots are immutable. Reload is atomic and requires a strictly
increasing generation. Equal or lower generations are rejected as stale or
rollback attempts.

A bounded previous-generation overlap may be configured. Overlap expires against
UTC time. Explicit revocation is immediate across current and overlap snapshots.

### Durable security state

A dedicated local SQLite database stores the accepted token snapshot, optional
bounded overlap metadata and the administrative audit chain. Startup verifies the
entire chain and the stored token-state fingerprint before serving authenticated
requests. Invalid state fails closed.

### Administrative audit

Security and administrative records are append-only and HMAC-SHA-256 chained with
an in-memory audit key supplied by local configuration. Each record binds:

- sequence number;
- UTC timestamp;
- correlation ID;
- direct peer;
- principal and non-secret token ID when known;
- action;
- outcome;
- canonical non-secret details;
- previous record MAC.

Authentication failures, authorization denials, rate-limit decisions, token
reloads, revocations, rollback attempts and admitted administrative mutations are
audited. Token changes and their audit record commit in one SQLite transaction.
Existing application mutations require a durable pre-admission audit record; if
that write is unavailable, the application call is not executed.

### Compatibility

The hardened controller is explicitly configured. Legacy static-token mode remains
available temporarily for compatibility tests but is not considered accepted
control-plane hardening. Unauthenticated loopback mode remains a development mode
and is never described as an authenticated administrative boundary.

## Required evidence

- Python 3.12 and 3.13 focused tests;
- generation reload, overlap expiry, immediate revocation and rollback rejection;
- restart and audit-chain verification, including tamper detection;
- bounded rate recovery and bounded-memory eviction;
- request-size and SSE-concurrency rejection;
- direct-peer identity and forwarded-header rejection;
- fail-closed mutation admission when audit durability is unavailable;
- secret non-disclosure across SQLite, responses, exceptions and logs;
- inherited Market Data package/interface, CLI, UPDATE, BACKFILL, export, Web and
  Playwright gates.

## Explicit non-goals

- TLS termination or certificate lifecycle;
- trusted proxies or external bind;
- private exchange API access or request signing;
- order placement, execution, portfolio mutation or live trading;
- deployment, package publication, release tags or production readiness claims.
