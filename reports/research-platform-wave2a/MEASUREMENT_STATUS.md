# Wave 2A measurement status

Status: **BASELINE COMPLETE; SLO PROPOSED; NO PRODUCTION REMEDIATION AUTHORIZED**.

## Evidence

- Baseline semantic digest:
  `e582e80fcfcc0d1fe2c39dc9b580fa2951023b6ce0e7b54678d6aaa19767ec26`.
- Measurement head: `73861889717ab1a656700d778de4d8eec4833ac2`.
- Workflow run/job/artifact: `30750252004` / `91502905226` / `8834237653`.
- Artifact ZIP SHA-256:
  `7f45d846ff54581391a9b089132db2d3d0a0c92805172a135d2c7ee5303a6c5e`.
- Environment: CPython 3.12.13, macOS 27.0 arm64, no network, no warm-up.

## Baseline result

- Bounded scheduler: 269.30 candidates/s; admission p99 1.9225 ms; scheduling p99
  3624.157 ms; orchestration-overhead p99 20.082583 ms.
- Sequential scheduler: 134.57 candidates/s; scheduling p99 7318.842 ms.
- Results Aggregator: 446.93 packages/s; publication p99 6.611583 ms.
- Three-repeat CV: 5.08% scheduler and 2.03% publisher.
- Determinism: one semantic digest across each three-repeat workload.
- Restart recovery: 0.882041 ms; cancellation cleanup: 4.724625 ms.
- Peak RSS: below 85 MB; positive FD growth: zero; orphan processes/artifacts: zero.
- Duplicate admission: 1,000/1,000 suppressed; invalid admission: 1,000/1,000 rejected
  before Backtest side effects.
- Immutable conflict: rejected and quarantined; original entity preserved; conflicting entity
  not visible.

## Verdict

The measured local synthetic/governance workload passes the proposed workload-bound SLOs.
There is no reproduced pre-existing SLO breach and no profiled Stream A-owned bottleneck.
Therefore no production optimization was performed. These measurements are regression evidence,
not a production-capacity claim.

The named canonical strategy remains `INCOMPLETE` and fail-closed; issue #312 owns the missing
contracts. No live trading, private credentials, network access or raw dataset publication were
used.
