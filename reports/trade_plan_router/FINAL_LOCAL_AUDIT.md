# Decision Router — Audit Record

## Purpose

This record separates historical local evidence from the current
base-synchronized remote validation. It is not merge, release, publication or
live-trading authorization.

## Historical local evidence

Earlier isolated development runs recorded larger locally reconstructed test
surfaces and additional long-running profiles. Their damaged base64/gzip test
transport could not be reconstructed as a trustworthy exact remote tree.
Historical counts and wheel hashes are not current release evidence.

The obsolete recovery utilities, failed recovery diagnostics and failed
baseline report have been removed. Production source and the complete active
test suite are normal tracked files.

## Base synchronization

The Router branch incorporated integration commit
`b4860c94dafb1d398cb36088d16c32bfcd065fc4` through the normal,
history-preserving merge checkpoint
`d51ad56fe88d07fed0f1794240466a7d284e2712`.

Repository comparison against that integration commit reports `behind_by=0`.

## Exact-head validation

Validation run `30059995804` passed on the base-synchronized checkpoint.

Results:

- 349 tests passed;
- no failures, errors or skips;
- Ruff passed;
- strict MyPy passed across 13 production source files;
- compile and dependency checks passed;
- branch-aware coverage was 90.3750%;
- the hard 90% coverage threshold passed without reduction;
- exact pinned Entry Model, Protection & Target, Position Scaling and Risk
  Sizing contracts were installed and checked.

## Final gate

Final run `30059995782` passed on macOS and established:

- production static security and public type-surface audits passed;
- Bandit and `pip-audit` passed;
- the executable golden registry passed 30/30 scenarios;
- deterministic replay passed across four timezone/hash-seed profiles;
- serialization fuzz passed 5,000 cases;
- risk-reduction fuzz passed 5,000 cases;
- concurrent duplicate delivery, independent-package concurrency and soak
  checks passed;
- two Core wheel builds were byte-identical;
- two Router wheel builds were byte-identical;
- clean install, import, uninstall, reinstall and `pip check` passed.

Verified Router wheel pair SHA-256:

`9412e4142e02375017f487d3248780b28f9f1b1da2e8a8f1578ac78cc1d4d741`

## Base-sync cross-checks

The same base-sync checkpoint also passed:

- core fast CI `30059995793`;
- FVG fast CI `30059995785`;
- session-range fast CI `30059995791`;
- market-regime fast CI `30059995779`;
- runner-routing policy `30059995780`.

## Downstream boundary

`DownstreamPlanEnvelope` is the explicit Router handoff API. It binds the exact
canonical JSON to a payload SHA-256 and preserves plan/content/audit identity,
validity timestamps and ordered string OCC preconditions. Downstream executors
must not recalculate Router decisions or coerce this evidence into incompatible
integer versions.

Paper Execution PR #98 still requires its downstream adapter and cross-contract
tests. Unsupported reversal must remain a typed rejection until verified Paper
reversal sequencing exists.

## Remaining project-level work

The Router module gates and base synchronization are green. Remaining work is
outside the Router's internal validation surface:

- Paper Execution adapter implementation and integration tests;
- any separately required qualified heavy audit or full mutation-framework run;
- explicit merge/release approval from the integration owner.

## Verdict

**BASE-SYNCHRONIZED VALIDATED DRAFT CHECKPOINT.**

PR #95 remains Draft and is not authorized for merge, package publication,
release tagging or live execution.
