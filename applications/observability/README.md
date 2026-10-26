# Unified Observability

Issue #378 runtime-safety observability contract for the PAPER platform.

This package is a read-only projection layer. Component owners remain authoritative for lifecycle, execution, risk, reconciliation, recovery, persistence, strategy admission, and notification delivery. Observability never initializes or migrates owner persistence and never creates a second ledger, scheduler, risk engine, or runtime state machine.

## Truth boundary

- Health vocabulary is `HEALTHY`, `DEGRADED`, `BLOCKED`, `UNAVAILABLE`.
- Owner-native state/reason identifiers are projected, not recalculated.
- Missing, stale, malformed, or unreachable required owner state is fail-closed as `UNAVAILABLE`.
- Explicit owner safety blocks (including Risk Supervisor HALT/manual review) are projected as `BLOCKED`.
- `public_details` carries only explicitly selected operator-safe scalar metrics. It is capped at 32 stable keys; nested payloads, non-finite numbers, integers outside signed 64-bit range, multiline/control text, and oversized strings are rejected. Integrations must whitelist only facts already classified as public and must never place credentials, private paths, raw topology, or arbitrary owner payloads there.
- Component state references are opaque SHA-256 digests; internal owner records are not serialized through observability.
- All time-dependent evaluation receives an explicit timezone-aware UTC `now`; no wall-clock sampling is hidden inside projection logic.

Risk Supervisor and Trading Runner adapters expose only bounded public state/count metrics in addition to stable health/reason codes. Other components must use their accepted read-only owner contracts; where authoritative telemetry does not exist, observability must report `UNAVAILABLE` rather than infer or synthesize it.

## Alert boundary

`AlertEngine` is the single bounded transient alert/deduplication owner inside this projection layer. `OperatorAlert` values are immutable ACTIVE/CLEARED transition events with stable event identity. `ActiveAlertView` is a separate current-state read model for Control Plane/Web/CLI: it exposes the active condition ID, severity, reason codes, next action, latest evaluated timestamp and source-observation hash without creating a second cooldown/deduplication ledger. Reading `active_alerts` never advances cooldown state; a clean authoritative observation removes the view through the existing clear transition.

## Integration boundary

The canonical runtime implementation lands on `integration/modular-strategy-full-rc`. PR #371 is merged, and Issue #378 accepted the Control Plane/Web/CLI consumption of the existing owner-backed projection. UI integrations must consume this package rather than recreate observability truth in UI state. Current project sequencing is recorded in the root `ROADMAP.md` and `.github/HQ_CRITICAL_PATH.md`.

PAPER only. Final 2h/24h physical soak is outside this package and remains owned by the project acceptance workstream.

## Partial #380 baseline-binding foundation

This foundation is tracked as a separate review-required preparation workstream;
it does not change the blocked full #380 acceptance gate or its MS9/FUTURES
dependencies.

`baseline_binding` defines immutable identities for the accepted strategy,
candidate/configuration, audit decision, datasets, assumptions, PAPER runtime,
factual observations, and an explicit versioned server-owned policy. Its pure
validator checks exact identity compatibility, supported contract schemas,
UTC window boundaries, future observations, duplicate event references, and
explicit minimum sample counts. The inclusive observation window is
`[now - window, now]`; `now` is supplied by the caller.

`IDENTITY_BOUND` means only that the supplied identities matched and the
policy's explicit minimum observation counts were present. It does not mean
healthy, drift-free, statistically qualified, or approved for acceptance. Low
counts produce `INSUFFICIENT`; missing or incompatible owner evidence and
unsupported policy schemas produce `UNAVAILABLE`. This foundation computes no
drift statistic or score and invents no threshold.

The `VerifiedBaselineReader` and `VerifiedDriftPolicyReader` contracts are
trusted adapter boundaries, not cryptographic verification implemented here.
A production baseline adapter must verify the native Strategy Package using
its builder/verifier and validate the native audit decision with its audit
verifier; a policy adapter must verify the server-owned policy registry
record. Observation adapters must expose factual, bounded PAPER event
references. Hash equality in this package does not authenticate arbitrary
claimed hashes or payload contents. Native adapters, statistical dimensions,
evidence-backed thresholds and false-alarm behavior, durable bounded windows,
Control Plane exposure, security/privacy evidence, and the required soak
remain open work for #380/#373. The package tests use synthetic boundary
fixtures; they do not import or certify the native Factory/Audit verification
path.
