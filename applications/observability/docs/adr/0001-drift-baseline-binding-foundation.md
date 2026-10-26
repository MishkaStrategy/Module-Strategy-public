# ADR 0001: Drift baseline identity-binding foundation

Status: accepted for the partial #380 contract foundation

## Context

Issue #380 needs research-to-PAPER identity binding before it can report drift.
Observability is a dependency-free, read-only projection and must not replace
strategy admission, audit verification, runner state, or policy ownership.

## Decision

Add immutable baseline, factual-observation, and versioned-policy contracts to
Observability. A pure validator checks exact identity compatibility, explicit
UTC `now` and inclusive observation windows, supported schemas, duplicate and
future facts, and server-supplied minimum sample counts. It returns only
`IDENTITY_BOUND`, `INSUFFICIENT`, or `UNAVAILABLE`, stable reasons, counts, and
a deterministic report hash. `IDENTITY_BOUND` describes compatibility only;
it is not a health, drift, significance, or release-acceptance verdict.

Trusted adapters must obtain baseline evidence through the native Strategy
Package builder/verifier and native audit verifier, observations through
read-only factual PAPER owners, and policies through the server-owned policy
registry. Observability compares the resulting identities but does not
authenticate arbitrary hashes or payloads. This module adds no dependencies,
owner writes, defaults, scores, or statistical thresholds.

## Consequences and remaining work

The contracts make the trust boundary and binding semantics testable without
market data or Factory dependencies. Synthetic tests exercise that boundary;
they do not certify the native Factory/Audit path. Native adapters, statistical
dimensions and evidence-backed thresholds, multiple-testing behavior, durable
bounded windows, Control Plane exposure, privacy/security review, and soak
acceptance remain open under #380/#373.
