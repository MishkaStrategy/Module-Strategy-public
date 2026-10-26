# Optimization Wave 2A — Research Platform SLO

## Identity

- Parent stream: Wave 2F Strategy Governance and Canonical Composition
- Parent branch: `optimization/wave2-strategy-governance-slo`
- Parent exact head at branch creation: `c3eea068ff1d04c569ac28807102fc4f7918d610`
- Working branch: `optimization/wave2-research-platform-slo`
- Phase: measurement and contract validation

## Purpose

Measure the end-to-end Research Platform using the versioned Strategy Governance contracts and workload generator produced by Stream F. This stream must preserve the fail-closed canonical-composition gap: the named strategy `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short` is not executable until all exact module contracts exist and canonical loading succeeds.

## Scope

- inventory Research Runner, experiment orchestration, artifact production and Results Aggregator integration;
- consume Stream F schemas, workload generator, candidate/evidence identities and gap report without modifying Stream F ownership;
- measure experiment throughput, p50/p95/p99 latency, peak RSS, artifact growth, restart/recovery and deterministic outputs;
- prove immutable provenance from composition candidate through experiment and aggregated result;
- define reproducible workload-bound SLOs;
- perform no production optimization before a repeated baseline and confirmed bottleneck.

## Ownership

Primary ownership is limited to:

- `research/experiment_runner/**`;
- `modules/results_aggregator/**` only where directly required for Research Platform measurement and with interface compatibility preserved;
- Stream A-specific benchmarks, tests, reports and documentation.

Read-only without coordinator handoff:

- `research/strategy_audit_validation/**` and all Stream F identities/evidence;
- Backtest/Core runtime;
- trading rules and canonical strategy semantics;
- shared workflows and root configuration;
- Router, Signal Bot, Paper Execution, Trading Runner, Registry/LocalStorage and Market Data Control Plane.

## Required workloads

- 1, 10, 100 and 1,000 experiment candidates;
- repeated identical candidates;
- invalid/incomplete candidates rejected fail-closed;
- large evidence/artifact payload;
- interrupted experiment and restart;
- partial/corrupted artifact recovery;
- bounded concurrent experiment workers where already supported;
- aggregation of successful, rejected and failed experiments.

## Required measurements

- experiments per second;
- p50/p95/p99 experiment latency;
- peak RSS and CPU time;
- bytes per experiment and total artifact growth;
- restart overhead and recovery time;
- deterministic digest equality across repeated runs;
- duplicate suppression and result aggregation latency;
- failures by class and zero unauthorized canonical execution.

## Governance

Keep the pull request Draft. Do not merge, mark Ready, enable auto-merge, rebase or force-push shared history, modify `main`, publish packages, create tags/releases, deploy, use private exchange credentials or enable live trading without separate explicit authorization.
