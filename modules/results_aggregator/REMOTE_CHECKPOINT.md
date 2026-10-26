# Results Aggregator & Experiment Registry — Remote Checkpoint

Status: **Canonical component remediated; v0.6 staging validation pending.**

Repository: `mishkacher/Module-Strategy`
Canonical branch: `research/results-aggregator`
Canonical head: `b33be3492c670ee282f0be5c08fc5236ff772894`
Component evidence PR: `#185` (Draft)
Central v0.6 base: `bd0be2febcb8910968d9839b6ebd749196bf0edb`
Integration candidate: `integration-candidate/v0-6-results-aggregator`

Authoritative remediation evidence:

- exact remediation head: `f1947368a47a5a3d3db1990fbe8a4c8f5827db67`;
- permanent Fast run: `30166338450`;
- Python 3.12: success;
- Python 3.13: success;
- compile, Ruff and MyPy: success;
- full correctness and branch-aware coverage suites: success;
- coverage: 90%, required threshold 90%;
- deterministic security scan: success, zero findings;
- wheel build and clean installed-wheel schema smoke: success;
- Python 3.12 artifact digest: `sha256:0864d4ef6606bd0968e3790aeb00619192a4ce612ab47eded28d8bb9fc15103d`;
- Python 3.13 artifact digest: `sha256:cef8e2cb650c11b8292cb46af7bb22d059a6167f5ed8f66a18a517f98ffaea65`.

The component now fails closed for cross-thread SQLite instance reuse, isolates
query deadline state per registry instance, declares audited POSIX filesystem
requirements explicitly, and rejects permissively coerced SQLite scalar types.

The earlier placeholder transport commit was immediately reverted without force;
the cleaned canonical pre-merge tree at `44eac59c895aa8c441deda90c6af14c18c41b2f1`
was byte-identical to `05a616e87bb5cc10a420588b1480deec3dec7792`.

This checkpoint does not authorize merge to `main`, release tags, GitHub
Releases, production package publication, production strategy selection, or
live trading. Exact v0.6 candidate validation remains mandatory before central
non-force publication.
