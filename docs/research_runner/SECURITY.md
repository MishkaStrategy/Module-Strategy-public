# Research Runner security boundary

## Runtime boundary

Research Runner orchestrates deterministic local research. Its supported owner-control
transport is an HTTP service bound only to loopback (`127.0.0.1`, `localhost`, or `::1`);
non-loopback server hosts and client origins are rejected. It is a local-host API, not a
remote-bind service. The server is constructed around one already-existing runner and a
fixed experiment identity.

The transport uses distinct reader and operator bearer tokens. The reader token can read
that experiment's owner identity and status; the operator token can request execution of
that same pre-authorized experiment. Tokens must be 16–4096 characters and distinct, and
authorization comparison is constant-time. POST requests require `Content-Length`, are
limited to 128 body bytes, and accept only the empty JSON object; client responses are
read with an 8192-byte ceiling. Client timeouts are also bounded. These controls bound the
owner protocol; they do not make the endpoint a remote service or replace host security.

The Research Runner service exposes no exchange credential-loading, exchange request-signing,
or order-submission flow. Research specifications are parsed as data, not Python callables;
the runner coordinates official Backtest Engine execution but does not implement strategy or
financial metric calculations. Strategy execution remains owned by the authoritative
Backtest Engine adapter.

## Optimization-safe invariants

Production source must not use Python `assert` for validation or durable-state invariants.
Assertions disappear under `python -O`; all such conditions therefore raise typed, fail-closed
errors. A source-AST regression test enforces this rule for the complete package.

## Deterministic pseudo-randomness

Grid sampling, bootstrap indices, signal skipping, and Monte Carlo path permutations use
`random.Random` with content-derived semantic seeds. This is intentional reproducibility
logic, not cryptographic randomness. The four call sites carry narrow Bandit `B311`
annotations; no repository-wide exclusion is used.

## Persistence and SQL

SQLite values are parameterized. Queries with dynamic caller-provided SQL fragments are
forbidden. Non-terminal status detection uses a fixed query and fixed enum parameters.
Stored payloads and rows are validated explicitly and fail closed if corrupted or missing.

## Supply-chain evidence

The label-triggered `research-runner-security` workflow runs on the movable `fast` role and
produces retained evidence for:

- Bandit source analysis;
- scoped `detect-secrets`;
- strict runtime dependency vulnerability audit;
- CycloneDX environment SBOM;
- exact candidate/toolchain metadata and SHA-256 checksums.

Security tooling is intentionally outside runtime dependencies. The workflow records resolved
tool versions and audits the externally resolved runtime dependency (`PyYAML`); local Core and
Research Runner distributions are verified by the ordinary package and integration gates.
