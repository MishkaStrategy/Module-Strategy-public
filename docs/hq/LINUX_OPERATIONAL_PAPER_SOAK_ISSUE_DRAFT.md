# Proposed issue: Linux/VDS operational PAPER soak acceptance

**Draft only.** This is a proposed new/successor issue for owner review. It does not alter existing issue scopes, authorize a workload, or establish acceptance.

## Purpose

Define and accept the exact-candidate Linux/VDS operational PAPER soak evidence required for release. The owner selected Linux/VDS as the operational PAPER host, with separate mandatory Darwin acceptance. Current scoped issue search found no dedicated Linux/VDS operational-soak issue; #400 remains the broad supported-topology item and does not define the soak acceptance scope.

## Scope

- Define Linux/VDS host, runtime, service topology, owner identities/namespaces, data and state stores, authentication, network/resource limits, recovery behavior, and supported operator procedures for the candidate.
- Require a separately authorized 2-hour operational PAPER soak, then a separately authorized 24-hour operational PAPER soak, each against the exact frozen Linux/VDS candidate and only after the preceding gates pass.
- Define the telemetry, failure handling, stop conditions, and immutable evidence needed to evaluate each soak.
- Keep Darwin acceptance as a separate environment and evidence path. Existing #866 and #369 remain Mac/Apple Silicon scoped; #873 remains Darwin acceptance. None is silently retargeted or treated as Linux soak evidence.

## Dependencies and entry gates

All dependencies below are prerequisites to be accepted; this draft does not claim they are complete:

- #250 produces a durable independently signed `APPROVED_FOR_PAPER` decision and a strict-reloadable PAPER-only `StrategyPackage`.
- #400 accepts the Linux/VDS topology and exact deployment environment, or the owner creates/approves a successor topology scope.
- #933 accepts the applicable composition and owner/provisioning contract. Its current scope remains Mac-local Stage-1 unless the owner separately approves a scope change or successor.
- #387 release-lifecycle contract, verifier, trust root, artifact/source lineage, and platform-specific lock/build identities are accepted for Linux deployment.
- #395 strategy lifecycle owner, durable per-instance state and common execution fence are accepted and implemented; Trading Runner checks the fence before every effect.
- #396 portfolio scope and authoritative capital, reservation, reconciliation, and terminal-state inputs are accepted where applicable to the operational composition.
- Exact Linux/VDS runtime, package locks, owner/admin identities, credentials/authentication policy, stores, evidence sources, and clean-host install/recovery/rollback inputs are supplied and independently validated.

## Acceptance

- The owner approves this issue's scope, accountable owners, supported Linux/VDS environment, and measurable soak thresholds before execution.
- A clean-host installation and recovery exercise completes on the exact candidate; package identity, configuration, owner bindings, locks, and evidence sources match the approved manifest.
- The 2-hour soak is separately authorized and completes without violating PAPER-only controls, risk limits, lifecycle fencing, portfolio invariants, or operational stop conditions. Failures and interruptions are retained and classified; no silent retry resets the evidence window.
- After the 2-hour result is independently accepted, the 24-hour soak receives a separate explicit authorization and passes on the same frozen candidate/environment, or on a separately approved exact candidate with all affected evidence repeated.
- Independent reviewers accept each result against the published thresholds and confirm no unresolved safety, integrity, recovery, or evidence-binding failures.
- Darwin acceptance evidence is recorded separately and does not satisfy Linux deployment or soak criteria.

## Evidence to attach

- Owner approval, issue scope, responsible operators/reviewers, and the one-time authorization for each soak.
- Candidate commit/package digest, signed `APPROVED_FOR_PAPER`, strict-reload/replay record, dependency lock/build identities, host image/runtime inventory, and configuration/owner namespace bindings.
- Clean-host install, reconnect, recovery, rollback, and readiness records with source-bound identities.
- Timestamped telemetry and event/effect summaries for the full soak interval, risk/portfolio state snapshots, lifecycle-fence checks, storage/resource/network observations, stop/incident records, and independently signed terminal reports.
- Separate independent acceptance decisions for the 2-hour and 24-hour evidence, plus references to accepted #400/#933/#387/#395/#396 prerequisites.

## Guardrails and open owner choices

- This draft grants no SEARCH, HOLDOUT, PAPER, soak, CI, deployment, publication, merge, or release authority. No soak may start until every applicable dependency is accepted and a distinct exact-candidate owner grant is issued for that soak.
- Do not change #866 or #369 from their current Mac/Apple Silicon scopes, or #873 from Darwin acceptance, through this draft. Use an owner-approved new issue or explicit scope change.
- Keep open whether Darwin additionally requires a separate 2-hour PAPER soak and whether/how #866 is re-scoped or replaced; no decision is inferred here.
- #1190 remains CLOSED/STOP. No SEARCH or HOLDOUT work is implied by operational soak planning.
- Actions remain disabled pending verified runner/publisher isolation. This issue does not authorize CI or any external setting change.
- Owners must select any remaining platform-specific host/runtime, identity, store, authentication/evidence, lock/build, recovery, telemetry, and acceptance-threshold inputs before this issue is accepted.
