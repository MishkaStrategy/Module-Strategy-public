# Full-RC Acceptance Checkpoint — 2026-07-31

Status: **FEATURE-FROZEN — PAPER-ONLY RELEASE CANDIDATE VALIDATION**

This checkpoint supplements the canonical
`docs/integration/FULL_PROJECT_INTEGRATION_LEDGER.md` with the exact state reached
after integrating the registered historical-dataset Backtest Engine bridge.
It does not authorize a merge to `main`, package publication, deployment, private
exchange access, order placement, real portfolio mutation or live execution.

## Exact accepted source identity

- repository: `MishkaStrategy/Module-Strategy`
- full-RC branch: `integration/modular-strategy-full-rc`
- accepted combined source head: `4024ef20cb0fd7618e036e181cbd96ec863c77da`
- base branch: `main`
- base SHA at PR #265: `996671bce0c2f8d2670791918476631fd30efd4f`
- frozen Core: `0.4.2`
- PR #265: open, Draft and unmerged
- paper-only: true
- live execution: prohibited

## History-preserving bridge integration

PR #285 (`integration/historical-data-backtester-bridge`) was integrated by the
ordinary merge commit:

- merge commit: `4024ef20cb0fd7618e036e181cbd96ec863c77da`
- bridge parent: `656c3439edc1e17a6dfeab46cb68246a3e4cb4f8`
- squash: not used
- rebase: not used
- force-push: not used

The accepted bridge preserves immutable dataset identity
`dataset_id + dataset_version + dataset_hash`, validates descriptor, manifest,
checksum, schema, lineage, UTC/timeframe and continuity before consumption, and
records sanitized provenance without exposing local storage paths or raw market
bytes.

## Combined exact-head acceptance

All pull-request workflows launched for source head `4024ef20…` completed with
`success` or an expected conditional `skipped` result. Key runs:

| Gate | Run | Result |
|---|---:|---|
| v0.6 executable acceptance | `30597154918` | Python 3.12/3.13 PASS |
| Backtest Engine | `30597154903` | PASS |
| Trading Runner | `30597154866` | PASS |
| Paper Execution | `30597154955` | PASS |
| Position & Exit | `30597154921` | PASS |
| Results Aggregator | `30597154891` | PASS |
| Strategy Factory | `30597154899` | PASS |
| Strategy Factory hardening | `30597154911` | PASS |
| Strategy Factory restart soak | `30597154939` | PASS |
| Strategy Audit & Validation | `30597154888` | PASS |
| Market Data Ingestion | `30597154908` | PASS |
| Market Data external corpus | `30597155009` | PASS |
| Market Data durable update | `30597154877` | PASS |
| Market Data durable backfill | `30597154869` | PASS after infrastructure-only retry |
| Market Data CLI parity | `30597154909` | PASS after infrastructure-only retry |
| Market Data Web Console E2E | `30597154910` | PASS |
| Wave Analysis | `30597154950` | PASS after external TLS-timeout retry |
| Core | `30597154878` | PASS |
| Fast checks | `30597154965` | PASS |
| actionlint | `30597154898` | PASS |
| runner routing policy | `30597154863` | PASS |

The retries did not change the source SHA or weaken any gate:

1. two jobs initially received a self-hosted runner reporting `0 GiB` free and
   stopped before project installation;
2. Wave Analysis `pip-audit` initially encountered a TLS handshake/read timeout to
   PyPI and passed unchanged on the retry.

## Acceptance artifacts

v0.6 executable evidence bound to source head `4024ef20…`:

- Python 3.12 artifact `8780606753`, digest
  `sha256:70ba7aa3fc9cd21d7c67f30ef06e210518de437f6e507b86232817b1d6025447`;
- Python 3.13 artifact `8780760463`, digest
  `sha256:4750f9a2ec2ae4cb5c4eb3641a145b109fcd2e5087293a67e0f989e92c710ce0`.

## Feature freeze

Until the release-readiness gates below are complete, the full-RC branch accepts
only:

- acceptance evidence and documentation;
- fixes for demonstrated correctness, security, reproducibility or CI defects;
- exact-head validation workflow changes that do not weaken existing gates.

New strategy features, execution modes, exchange integrations, live credentials,
external binds and unrelated refactors are out of scope.

## Remaining gates before Ready review

1. Run persistent registered-dataset → Backtest Engine smoke on the current combined
   full-RC exact head, not only on the pre-merge bridge head.
2. Run full-RC release-readiness audit for tracked artifact hygiene, paper-only
   boundary, targeted registry/backtest/auditor/factory contracts and sanitized
   evidence generation.
3. Record the new exact-head artifacts and digests in PR #265.
4. Re-run any affected project-wide matrix after acceptance-only workflow changes.
5. Obtain explicit authorization before moving PR #265 from Draft to Ready.
6. Obtain separate explicit authorization before any merge to `main`.

## Fail-closed boundary

This checkpoint is research and paper-runtime evidence only. It is not a claim of
profitability, production readiness or live-trading fitness. Any missing identity,
lineage, checksum, audit decision, risk evidence or paper-only constraint must stop
admission rather than fall back to local paths, mutable aliases or permissive
execution.
