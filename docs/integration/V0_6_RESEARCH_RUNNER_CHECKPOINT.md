# Modular Strategy v0.6 — Research Runner checkpoint

Status: **integration candidate; exact central-candidate validation required**.

## Identity

- Repository: `mishkacher/Module-Strategy`
- Frozen Core SHA: `cc7eb494c27eb0fc48dd16026b704bade9f6de3e`
- Prior central checkpoint: `f791a9d3bdc58eaa6073b578ac54a0c27813b880`
- Canonical component branch: `research/experiment-runner`
- Canonical component head: `97b4cf512b816a24042f07f40314d010fde666a7`
- Candidate merge commit: `4c8effb3f3d8aa379f9b8a4ec146fd3eacbb75ad`
- Integration candidate branch: `integration-candidate/v0-6-research-runner`
- Component evidence PR: `#119` (Draft)

## Exact component evidence

All listed runs are bound to canonical head `97b4cf512b816a24042f07f40314d010fde666a7`.

- focused quality gate: run `30079675230`, success;
- full Python 3.12/3.13 matrix: run `30079729837`, success, 376 tests per version;
- centralized integration: run `30079729778`, success on Python 3.12/3.13;
- security and supply chain: run `30079988369`, success;
- release and reproducibility: run `30080067339`, success on Python 3.12/3.13;
- heavy/recovery scale 5: run `30080148989`, success.

The full matrix reported 92.0876% combined line/branch coverage. The reproducible wheel SHA-256
was `fd84ac6d397300e2eccd865ff07856196e8e983c8f5918c34902cdde3a5ecafa`; the normalized
sdist SHA-256 was `b83dbb9dd7a7ba5165587d7b31631dcd79b9fcf602017ae8d49194a94c1cf07a`.

## Remediated architecture findings

1. Durable claims are fenced by scheduler session identity, opaque lease token, worker identity,
   exact claim revision, and lease expiry. Late stale mutations after recovery/reissue fail closed.
2. Backtest output verification is bound to a bounded sidecar manifest, exact run identities,
   stable regular-file hashes, exact inventory, artifact-count and byte ceilings.
3. Experiment cancellation is serialized against durable result publication and actively cancels
   or fences already-running futures.
4. Heartbeats independently validate positive lease duration and advance durable claim revision.
5. New schema creation and v1-to-v2 migration execute statement-by-statement inside one
   `BEGIN IMMEDIATE` transaction; fault-injection regressions prove complete rollback.

## Integration ownership

The candidate adds only `research/experiment_runner/**`, `docs/research_runner/**`, four
dedicated Research workflows, this checkpoint, and a local Research `.gitignore`. Exact central
blobs remain authoritative for root `.gitignore` and shared `.github/workflows/integration-ci.yml`.
The authoritative Backtest Engine and Results Aggregator component branches remain external
validation inputs to Research Runner CI.

## Safety

This checkpoint is not a release declaration. Central PR `#168` must remain open and Draft.
Do not merge to `main` or frozen Core, force-push, rewrite history, publish packages or releases,
select a production strategy, or enable live execution. Publish to the central v0.6 branch only
after the exact candidate full Python 3.12/3.13 gate succeeds and ancestry remains a clean
non-force fast-forward.
