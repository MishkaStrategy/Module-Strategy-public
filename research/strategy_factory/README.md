# Automatic Strategy Builder / Strategy Factory

`strategy_factory` is the high-level, deterministic research planner for the Modular Strategy project. It decides **what to research and why**, delegates reproducible execution to Research Runner, reads canonical evidence from Results Aggregator, requires an independent audit decision, and can produce a paper-only Strategy Package.

> Current checkpoint: the create, adapt, improve, and validate stage machines are implemented locally, including real Research Runner planning/execution, Backtest Engine output verification, Results Aggregator registration, baseline-aware promotion, walk-forward, robustness, one-time holdout, audit port, paper-only package assembly, recovery, CLI, and observability. The branch-aware coverage threshold of 90% and the explicit 25-scenario acceptance package pass locally. The overall release gate is **not passed**: Strategy Factory Ruff/MyPy and remaining hardening gates are pending, and independent Strategy Audit & Validation remains an external blocker.

## Ownership boundary

| Component | Owns |
|---|---|
| Strategy Factory | dataset gate, instrument profile, hypotheses, architecture grammar, experiment plans, pruning/promotion decisions, package assembly |
| Research Runner | deterministic job planning, search execution, retries, resume, one-time untouched test grant |
| Backtest Engine | bar simulation, orders/fills, accounting, trade/equity evidence |
| Results Aggregator | immutable experiment registry, metrics, slices, lineage, Pareto/evidence packages |
| Strategy Audit & Validation | independent approval/rejection decision |
| Trading Runner | accepted paper strategy runtime |
| Paper Execution | paper orders, fills, OCO, reconciliation |

Strategy Factory is not a backtester, broker, metric calculator, or live execution service.

## CLI

```bash
strategy-factory doctor
strategy-factory create --config configs/examples/ethusdt-create.yaml \
  --commit-sha 9b7cc5cac837f8d66c691117143e0dcb9cf2d8f8 \
  --interval-seconds 300
strategy-factory status --run-id sf-run-...
strategy-factory inspect --run-id sf-run-...
strategy-factory resume --run-id sf-run-...
strategy-factory cancel --run-id sf-run-...
strategy-factory export --run-id sf-run-... --artifact-id dataset_validation-... \
  --output ./dataset-validation.json
```

The foreground application owns durable create/status/inspect/resume/cancel/export operations. Full research execution is assembled through typed ports so unavailable Research Runner, Results Aggregator, audit, or package-source dependencies block explicitly rather than being replaced by simulated results.

## Local verification

```bash
PYTHONPATH=src python -m compileall -q src
PYTHONPATH=src pytest -q
PYTHONPATH=src coverage run --branch --source=strategy_factory -m pytest -q
coverage report -m
python -m pip wheel --no-deps . -w dist
```

The package requires Python 3.12+ and the exact compatible Core, Research Runner, and Results Aggregator packages.

## Safety

- no live exchange adapter;
- no secret fields in Strategy Package;
- no generated Python execution;
- no holdout use for tuning;
- no automatic production promotion;
- paper admission requires robustness + one-time holdout + independent audit evidence;
- the current implementation fails closed because Strategy Audit & Validation is unavailable.
