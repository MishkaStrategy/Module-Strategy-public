# Decision Router remote synchronization status

Repository: `mishkacher/Module-Strategy`

Base branch: `integration/modular-strategy-v0.5.0`

Latest integration commit incorporated into the Router branch:
`b4860c94dafb1d398cb36088d16c32bfcd065fc4`

History-preserving base-sync checkpoint:
`d51ad56fe88d07fed0f1794240466a7d284e2712`

Working branch: `modules/trade-plan-router-v0.5`

Draft PR: `#95`

## Verified remote gates

Validation run `30060866166` passed:

- tests: **349 passed**, no failures, errors or skips;
- Ruff: **PASS**;
- strict MyPy: **PASS** across 13 production source files;
- branch-aware coverage: **90.3750%** with a hard 90% gate;
- compile and dependency checks: **PASS**;
- pinned Entry, Protection, Scaling and Risk contracts installed from exact
  commits.

Final validation run `30060866133` passed:

- production security scan, type-surface audit, Bandit and `pip-audit`: **PASS**;
- executable golden registry: **30/30**;
- deterministic replay: **PASS** across four timezone/hash-seed profiles;
- serialization and risk-reduction fuzz: **10,000 total cases, PASS**;
- concurrency/soak stress: **PASS**;
- Core and Router wheel pairs: **byte-identical**;
- clean install, import, uninstall, reinstall and `pip check`: **PASS**.

Project-wide fast checks also passed:

- core fast CI: `30060866146`;
- FVG fast CI: `30060866178`;
- session-range fast CI: `30060866132`;
- market-regime fast CI: `30060866137`;
- runner-routing policy: `30060866174`.

Repository comparison against the integration branch reports `behind_by=0`.

## Validated Core 0.4.2 compatibility

Compatibility run `30060866128` passed against the fully validated Core
candidate `04f6de01e5337883256299dd20effdbedd5ac73e`:

- Core version: `0.4.2`;
- Router version: `0.1.0`;
- Router tests: **349 passed**;
- Ruff and strict MyPy: **PASS**;
- branch-aware Router coverage: **90%**;
- package dependency check: **PASS**;
- Core 0.4.2 + Router clean wheel installation/import: **PASS**.

Compatibility wheel SHA-256 values:

- Core 0.4.2: `392d1c0fc1b91118d87cafef3999f094342e7476d0fcf98be3a2bb44027eb9c1`;
- Router 0.1.0: `6a914faeb8b6bc3d4e6d4a380bf82f3d52388749255f5cae894cf17a0d6a4247`.

This gate proves forward compatibility with the accepted Core 0.4.2 candidate.
It does not replace the integration-base validation or merge Core PR #96.

## Downstream contract

`DownstreamPlanEnvelope` is the explicit Router handoff boundary. It preserves
canonical Router JSON, a SHA-256 digest of the delivered payload, plan/content/
audit identity, validity timestamps and exact ordered string OCC
preconditions. It does not convert Router versions into Paper Execution integer
`StateVersions`.

## Synchronization state

Production source and tests are normal tracked files. Corrupted base64/gzip
recovery utilities, failed recovery diagnostics and the failed baseline report
have been removed from the active branch.

Router workflows use isolated checkouts with bounded install retries/timeouts
and compact evidence artifacts. This prevents other module jobs from
contaminating Router validation workspaces.

This PR remains Draft. No merge, force-push, release tag, package publication,
live-trading activation or auto-merge is authorized by this checkpoint.
