# Integration boundary

Status: **STAGED V0.6 INTEGRATION CANDIDATE — NOT RELEASED / NOT PRODUCTION**.

Repository: `mishkacher/Module-Strategy`
Canonical component branch: `research/results-aggregator`
Canonical component head: `b33be3492c670ee282f0be5c08fc5236ff772894`
Central integration base: `bd0be2febcb8910968d9839b6ebd749196bf0edb`
Integration candidate branch: `integration-candidate/v0-6-results-aggregator`
Component evidence PR: `#185`

The complete Results Aggregator & Experiment Registry source is present under
`modules/results_aggregator/`. The component remains an immutable consumer and
registry of verified research/backtest artifacts. It does not execute trading
strategies, alter producer results, select a production strategy, or duplicate
the Core backtest/metric engine.

The post-audit remediation is fail-closed for SQLite thread ownership, query
deadline isolation, POSIX storage capabilities, and strict scalar decoding.
Exact component head `f1947368a47a5a3d3db1990fbe8a4c8f5827db67`
passed the permanent Python 3.12/3.13 Fast matrix in run `30166338450`.

Integration into the v0.6 candidate must preserve central ownership of shared
CI, policy, routing and root packaging files. The Results-owned module tree and
dedicated Fast workflow may be staged only after exact-candidate validation.

This document does not authorize merge to `main`, force-push, history rewrite,
release tags, GitHub Releases, package publication, production selection, or
live trading. Central PR `#168` remains the Draft staging surface.
