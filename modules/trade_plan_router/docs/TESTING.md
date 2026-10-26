# Testing and validation

## Exact-head validation gate

The GitHub validation workflow checks the exact Router PR head inside an
isolated `router-worktree` checkout. It installs exact pinned Entry Model,
Protection & Target, Position Scaling and Risk Sizing contracts before running:

1. Python 3.12+ verification;
2. package dependency checks;
3. compileall for production source and tests;
4. Ruff;
5. strict MyPy;
6. the complete pytest suite;
7. branch-aware coverage with a hard 90% minimum;
8. evidence upload, atomic evidence publication and final enforcement.

Verified run `30060866166`:

- 349 tests passed;
- no failures, errors or skips;
- Ruff passed;
- strict MyPy passed across 13 production source files;
- branch-aware coverage was 90.3750%;
- the 90% threshold passed without reduction.

## Final validation gate

The final workflow uses the same isolated checkout and runs:

1. production static security scan;
2. public type-surface audit;
3. Bandit;
4. `pip-audit`;
5. executable 30-scenario golden registry;
6. deterministic replay under four timezone/hash-seed profiles;
7. deterministic serialization and risk-reduction fuzzing;
8. concurrency, duplicate-delivery and soak stress;
9. two independent Core and Router wheel builds with pairwise SHA-256 equality;
10. wheel-content policy;
11. clean install, import, uninstall, reinstall and `pip check`.

Verified final run `30060866133` passed all stages.

## Validated Core 0.4.2 compatibility

`decision-router-core-v042-compatibility.yml` independently checks the exact
Router head against the fully validated Core candidate
`04f6de01e5337883256299dd20effdbedd5ac73e` while retaining the four exact
upstream module contracts.

The gate runs on the allowed `[self-hosted, fast]` role and uses a
cross-platform Python driver for:

1. exact package-version verification;
2. compileall, Ruff and strict MyPy;
3. the complete Router pytest suite;
4. branch-aware coverage with a hard 90% minimum;
5. Core 0.4.2, Router and PyYAML wheel creation;
6. clean no-index installation, `pip check` and public import smoke.

Verified compatibility run `30060866128`:

- Core `0.4.2` exact head `04f6de01...`;
- Router `0.1.0` exact workflow/source head `e64b7801...`;
- **349 passed** in 9.21 seconds;
- Ruff and strict MyPy passed;
- branch-aware coverage passed at 90%;
- clean Core 0.4.2 + Router wheel installation/import passed;
- dependency check reported no broken requirements.

Wheel SHA-256 values from the compatibility run:

- Core: `392d1c0fc1b91118d87cafef3999f094342e7476d0fcf98be3a2bb44027eb9c1`;
- Router: `6a914faeb8b6bc3d4e6d4a380bf82f3d52388749255f5cae894cf17a0d6a4247`.

This result proves forward compatibility with the accepted Core candidate; it
does not replace integration-base validation or merge Core PR #96.

## Integration-base cross-check

The Router branch incorporated integration commit
`b4860c94dafb1d398cb36088d16c32bfcd065fc4` through merge checkpoint
`d51ad56fe88d07fed0f1794240466a7d284e2712`. Comparison reports
`behind_by=0`.

The current workflow checkpoint also passed:

- core fast CI `30060866146`;
- FVG fast CI `30060866178`;
- session-range fast CI `30060866132`;
- market-regime fast CI `30060866137`;
- runner-routing policy `30060866174`.

## Local Fast CI

```bash
./scripts/local_fast_ci.sh round-name
```

The local script performs an equivalent workspace-oriented package lifecycle
check when the required Core/upstream source paths and local dependency wheels
are supplied. Local evidence must not replace exact-head remote evidence.

## Test classes

- unit and invariant tests for domain models and policies;
- actual upstream contract normalizer tests;
- executable golden Long/Short/position/pending/scaling/reversal scenarios;
- seeded permutation and metamorphic tests;
- concurrent duplicate-delivery and independent-package replay;
- malformed and adversarial payload tests;
- re-signed invalid serialized plans/results proving hashes cannot bypass nested
  schema, time, geometry, allocation, OCC or ordering validation;
- recursion, cycle, Unicode, log-injection and resource-limit tests;
- Module API adapter, manifest and state tests;
- downstream handoff envelope, payload-hash and exact OCC preservation tests;
- fault injection proving unexpected exceptions remain visible;
- performance, memory, fuzz and package lifecycle checks.

## Deterministic standalone-exit fuzzing

`benchmarks/fuzz_risk_reduction.py` generates valid randomized position,
scaling, lock, freshness and pending-order combinations. It routes each package
twice and asserts byte-stable serialization plus reduce-only, zero-risk, exact
position-target and REDUCE/CLOSE quantity invariants.

The latest final gate executed 5,000 seeded risk-reduction cases. The benchmark
CLI supports larger explicit case counts for separate heavy campaigns.

## Serialization fuzzing

`benchmarks/fuzz_serialization.py` applies deterministic text, byte and nested
JSON mutations to accepted and rejected routing-result payloads. Every mutated
payload must either return a valid immutable mapping that survives canonical
replay or fail with `RouterSerializationError`; raw parser, Unicode, type or
internal exceptions may not escape the public boundary.

The latest final gate executed 5,000 seeded serialization cases. Together with
the risk-reduction workload, the verified final profile covered 10,000 cases.

## Concurrency and soak profile

`benchmarks/stress_router.py` exercises a shared stateless Router under
concurrent duplicate delivery, independent concurrent packages and replay soak.
It requires exact result-ID and canonical-serialization stability and records
throughput plus `tracemalloc` retained/peak memory.

These profiles are regression evidence, not production latency/memory SLOs.

## Fault injection

Focused tests inject failures into freshness validation, portfolio evaluation,
standalone composition and serialization boundaries. Expected
`PlanInvariantError` failures become typed `PLAN_INVARIANT_FAILED` results;
malformed internal evaluator contracts and unexpected runtime failures remain
visible instead of becoming silent business rejections.

## Golden fixture matrix

`tests/data/golden_fixtures_v1.json` is the versioned registry of 30 mandatory
scenarios. `test_golden_registry_scenarios.py` executes every row and verifies
its verdict, action/reason or deterministic invariant.
`scripts/verify_golden_matrix.py` validates schema, sequential IDs, expected
verdicts, pytest-node existence and fixture count.

## Downstream handoff tests

`test_downstream_handoff.py` verifies that `DownstreamPlanEnvelope`:

- preserves canonical JSON and plan/content/audit identity;
- binds delivered bytes to `payload_sha256`;
- preserves ordered string preconditions without integer coercion;
- rejects payload, identifier and precondition tampering;
- is idempotent for identical plans;
- rechecks validity windows and supported executor actions.

Paper Execution still requires its own adapter and cross-contract tests.

## Remaining project-level gates

The Router's internal validation, final gate, integration-base checks and Core
0.4.2 compatibility gate pass. The following must not be represented as
complete Router-to-system integration:

- Paper Execution adapter implementation and unsupported-reversal handling;
- any separately mandated qualified heavy/backtester audit;
- a full mutation-framework campaign, if required by project release policy;
- explicit merge/release approval.

## Release-gate evaluator

`scripts/release_gate.py` verifies required Fast CI files, coverage, benchmark,
fuzz, stress, package lifecycle and optional external reports. Missing required
project-level evidence remains an explicit blocker rather than an inferred
success.
