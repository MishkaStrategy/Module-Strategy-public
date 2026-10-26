# MS5A Owner-Backed Multi-Strategy SEARCH Contract

## Authority and scope

Owner decision recorded in #1226 authorizes this implementation branch from exact accepted basis:

`integration/modular-strategy-full-rc@52592823580cd9aa3dcc43602b730aa9bc2e333f`.

The authority is implementation-and-test only. It does not authorize a SEARCH execution, WALK_FORWARD/ROBUSTNESS, HOLDOUT/final-test, FINAL_AUDIT, PAPER, LIVE/private execution, force-push/history rewrite, or unrelated lineage consolidation.

GOV-SEARCH-001 #1223 remains a separate downstream one-shot authority.

## Product boundary

MS5A must produce one runnable owner-backed multi-strategy SEARCH harness for exactly these initial SIGNAL families:

- `breakout_return`
- `liquidity_sweep`
- `fvg_rt`

The harness must stop before `WALK_FORWARD`. Reaching or crossing WALK_FORWARD/ROBUSTNESS, HOLDOUT, FINAL_AUDIT, PACKAGE or PAPER_ADMISSION is a contract violation.

## Owner dataset identity

The historical owner-backed contract is retained as the evidence basis for the new multi-family request:

- dataset id: `binance_usdt-m-perpetual_btcusdt_5m`
- dataset version: `1`
- symbol: `BTCUSDT`
- execution exchange: `binance_usdtm`
- market type: `LINEAR_FUTURES`
- base timeframe: `5m`
- required/allowed timeframes: `5m, 15m, 1h, 4h, 1d`
- tested-period lower bound: `2023-11-10T04:05:00Z`
- Kline owner hash: `49ee948c7c22dc3c552bf6a96d2fdce9c5bba97662a694ae98ab85fc521c04f5`
- dataset period end: derived fail-closed from the verified overlap of registered Kline, funding and mark-price coverage
- file format exposed to Strategy Factory: `parquet`

The owner adapter must deep-verify the live registered dataset and refuse hash, identity, coverage or gap-policy drift.

## Request semantics retained from the proven owner contract

Unless later code evidence proves a compatibility issue, the multi-family request retains:

- mode: `CREATE`
- initial capital: `10000`
- research risk fraction: `0.005`
- maximum drawdown fraction: `0.15`
- minimum trade count: `30`
- deterministic random seed: `312`
- immutable request `created_at`: `2026-09-23T16:21:06Z` (creation time of authorized Draft PR #1227)
- objectives: `profit_factor=maximize`, `max_drawdown=minimize`
- accepted OOS execution-cost stress policy from the owner-backed research contract

The final accepted request must be persisted by immutable canonical fingerprint.

The mandatory non-searchable runtime closure is also source-bound and fingerprinted. Grammar must consume its explicit runtime feature contracts; `instrument.rules` and `portfolio.risk_snapshot` may not be admitted through an external-feature escape hatch. This preserves fail-closed producer/dependency/conflict/cycle validation across the searchable architecture plus the owner-backed runtime closure.

## Research split and HOLDOUT boundary

The Strategy Factory default research split is deterministic `60/20/10/10` over the request dataset period:

- 60% train
- 20% validation
- 10% test
- final 10% untouched HOLDOUT

SEARCH may use the pre-HOLDOUT planning metadata needed by Research Runner, but MS5A execution must not request a final-test grant, execute HOLDOUT, or use HOLDOUT outcomes for selection.

## Fairness requirements discovered by live audit

The accepted family-first structural-screening allocator is necessary but not sufficient for a real multi-family request.

MS5A must additionally prevent two ordering/budget leaks:

1. **Architecture truncation bias.** Generic Grammar stops after `maximum_architectures` accepted architectures. A multi-family harness must not let enumeration order omit one of the three families before family-balanced experiment allocation starts.
2. **Promotion multiplication.** Generic coarse/local promotion uses one stage limit when selecting candidates from each plan. A multi-plan SEARCH must ensure the global coarse/local stage budget is allocated across families/plans rather than accidentally applied independently to every plan.

Both properties are implemented as explicit opt-in family-balanced architecture and promotion policies and are covered by focused tests before the numeric ResourceBudget is accepted.

SEARCH finalization is also part of the MS5A boundary. After LOCAL_REFINEMENT completes, all terminal experiment populations are ranked together under one global Pareto policy using the common minimum-trade and maximum-drawdown constraints. The resulting finalist set is frozen durably before WALK_FORWARD. WALK_FORWARD must not choose a new finalist set as a side effect of validation.

A family/plan may legitimately produce zero eligible promotion candidates. In the family-balanced MS5A path this is retained as evidence rather than failing the whole SEARCH. If the global post-refinement ranking has no admissible candidates, SEARCH completes with `no_admissible_candidate`; it does not retry automatically and it creates no WALK_FORWARD authority.

## Exact ResourceBudget freeze

The historical single-family budget (`128` maximum experiments, one architecture, one finalist, 86400 seconds, 8 GiB, one CPU worker, one parallel task) is evidence only and is not copied blindly.

The frozen numeric MS5A budget is:

- `maximum_experiments = 132`
- `maximum_architectures = 12` — 4 slots per family
- `maximum_finalists = 3`
- `maximum_duration_seconds = 86400`
- `maximum_memory_bytes = 8589934592` (8 GiB)
- `maximum_cpu_workers = 1`
- `maximum_parallel_tasks = 1`
- stage limits:
  - `STRUCTURAL_SCREENING = 96` — 32/family, nominally 8/architecture at the 12-architecture cap
  - `COARSE_SEARCH = 24` — 8/family, nominally 2/architecture
  - `LOCAL_REFINEMENT = 12` — 4/family, nominally 1/architecture
- no WALK_FORWARD or HOLDOUT experiment allocation exists in this request.

Unused family/plan capacity is not redistributed. The stage limits sum exactly to the global experiment ceiling (96 + 24 + 12 = 132). This keeps the research allocation deterministic and prevents one family from winning additional capacity because another family has a smaller valid search surface.

The resulting live request fingerprint still depends on the deep-verified registered dataset period/metadata and must be captured against the accepted exact SHA before #1223 is asked for SEARCH authority. The automatic `ms5a-owner-search-preflight.yml` workflow runs synthetic compatibility tests only; it does not resolve an owner dataset or produce owner-data evidence. The owner preflight script remains available, but this workflow does not invoke it.

Any future data-backed preflight requires a separately approved, explicit procedure with fresh owner authorization bound to the exact candidate SHA. That procedure must use an isolated raw-byte catalog copy, keep original payloads read-only, and never open the original catalog SQLite. Preflight does not itself authorize SEARCH or backtesting. No PR-head evidence can substitute for evidence bound to the accepted post-merge SHA.

## Evidence required before #1223

The accepted MS5A SHA must prove:

- exact Git source SHA and clean-tree provenance;
- exact owner dataset identity and deep verification;
- exact request fingerprint and numeric ResourceBudget;
- every requested family is represented before SEARCH starts;
- family-neutral structural, coarse and local capacity accounting;
- canonical Research Runner / Results Aggregator integration;
- durable experiment/run identities and terminal evidence;
- restart/idempotency and exactly-once behavior;
- zero HOLDOUT/final-test grants;
- hard stop before WALK_FORWARD;
- sanitized evidence with no secrets/private paths;
- focused unit/integration/security/privacy checks plus exact-head CI.
