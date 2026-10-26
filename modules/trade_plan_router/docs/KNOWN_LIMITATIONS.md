# Known limitations

## Module scope

- The Router composes deterministic plans but does not submit, cancel or
  reconcile exchange orders.
- The Module API adapter consumes one symbol per `MarketView`; cross-symbol
  portfolio arbitration requires an upstream coordinator or a future explicit
  multi-symbol package contract.
- Portfolio snapshots are trusted only after Router schema/invariant checks.
  The Router does not independently reconcile them with an exchange or venue.
- Risk-reducing lock override is policy controlled and still requires explicit,
  versioned position targeting.

## Schema and compatibility

- Canonical JSON deserialization supports plan/result schema `1`. Future schema
  changes require explicit versioned adapters.
- The adapter accepts canonical `entry_model.plan` and a bounded legacy alias,
  but compatibility handling does not remove the need for upstream feature-key
  alignment.
- `DownstreamPlanEnvelope` preserves exact string/SHA-256 OCC evidence. Paper
  Execution's provisional integer `StateVersions` cannot replace or silently
  coerce these Router preconditions.
- Paper Execution PR #98 still requires an explicit adapter from the canonical
  Router plan/envelope to its internal execution models.
- Router can compose `REVERSE_POSITION`; Paper currently lacks verified reversal
  sequencing and must typed-reject that action until implemented.

## Validation boundaries

- Exact-head validation currently passes 349 tests with 90.3750% branch-aware
  coverage, Ruff and strict MyPy.
- The final macOS gate passes security/type audits, Bandit, `pip-audit`, 30/30
  golden scenarios, four-profile replay, 10,000 fuzz cases, stress, reproducible
  wheels and clean package lifecycle checks.
- The final-gate performance and stress profiles are regression evidence, not a
  production latency or memory SLO.
- A separate full mutation-framework campaign has not been claimed as passed.
- Any project policy requiring a qualified heavy/backtester audit remains a
  project-level integration gate rather than an inferred Router result.
- Final behavior must still be reviewed against the integration target and the
  downstream Paper adapter before merge.

## Operational status

- Self-hosted Router workflows use an isolated `router-worktree` checkout with
  nested pinned upstream contracts, bounded install retries/timeouts and compact
  evidence artifacts.
- The old corrupted base64/gzip recovery utilities and failed reports have been
  removed. Recovery is not part of the active validation path.
- PR #95 remains Draft. No force-push, merge, release tag, GitHub Release,
  package publication or live-trading activation is authorized.
