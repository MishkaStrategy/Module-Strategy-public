# Research Runner & Parameter Optimization — Baseline Report

Date: 2026-07-23
Repository: `mishkacher/Module-Strategy`
Working branch: `research/experiment-runner`
Base branch: `integration/modular-strategy-v0.5.0`
Base and initial head: `5e03b933207a99940244634f49a756d27888a5c3`
Backtest Engine reference branch: `engine/modular-backtest`
Backtest Engine reference commit: `2198df1559d91a7ad2982a86bab4ddde152eb6fe`

## Working-tree state before product changes

- working tree: clean;
- working branch existed remotely and was identical to the approved base;
- no Research Runner product commit existed;
- no Draft Pull Request for `research/experiment-runner` existed;
- no parallel experiment platform was found in the authoritative repository tree.

## Existing contracts

The integration base exposes strategy-independent extension protocols in
`modular_core.extensions`:

- `ParameterOptimizer.generate()`;
- `ScenarioGenerator.generate()`;
- `DistributedExecutor.map()`;
- `ResearchRunner.run()`.

These are intentionally minimal protocols and are not an experiment platform.

The authoritative Backtest Engine implementation exists on
`engine/modular-backtest` and exposes:

- `modular_backtest.BacktestAppConfig`;
- `modular_backtest.run_backtest()`;
- `modular_backtest.RunOutcome`;
- deterministic replay and restart verification;
- application and source provenance;
- existing Core and extended metrics artifacts;
- atomic result publication and artifact verification.

The Research Runner must call that application contract. It must not copy its event
loop, data ingestion, execution simulation, trade accounting, equity calculation or
metrics implementation.

## Baseline checks

Environment:

- Python: 3.13.5 available locally;
- project requirement: Python 3.12+;
- pytest: 9.0.2;
- Ruff and MyPy executables were not preinstalled in the local container.

Initial root test attempt:

- 243 tests passed;
- one integration smoke failed because the independent Session Range, Market Regime
  and FVG distributions had not been installed and their entry points were absent;
- classification: environment/configuration issue, not an implementation failure.

Corrected isolated local environment:

- root package installed editable without network access;
- official Session Range, Market Regime and FVG packages installed editable;
- root/integration suite: **244 passed**;
- deterministic integration smoke: **PASS**;
- smoke run id: `run-a913a83ae17411b4`;
- Backtest Engine focused suite at exact engine commit: **113 passed**.

## Baseline gaps

At baseline the repository did not contain:

- typed research specification and immutable experiment manifest;
- dataset-lineage contract for research orchestration;
- deterministic experiment/run ID factories;
- documented seed hierarchy;
- grid or random search planners;
- train/validation/test split planner;
- leakage guard or test-access audit;
- experiment artifact/state/checkpoint store;
- candidate ranking and promotion gate;
- walk-forward, robustness, bootstrap or Monte Carlo orchestration;
- Research Runner package, tests, CLI or documentation.

## Initial verdict

`LOCAL DEVELOPMENT — NOT READY`

The first implementation checkpoint may establish orchestration foundations only.
It must not claim completion of the five completeness rounds, thirty hardening
rounds, fifteen audits, Heavy Research Audit or release gate.
