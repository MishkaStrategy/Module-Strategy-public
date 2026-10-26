# Local computer testing gate

This procedure prepares the canonical deterministic paper-only v0.6 tree for
historical tests on macOS or Linux. It does not enable live exchange access.

## Validated baseline

The tooling is based on integration checkpoint:

```text
3962fe5116c05ca9858bc9bcaf03a6e0d84c9e66
```

The local checkout may be that exact commit or a descendant. The preflight
fails when the canonical checkpoint is not in `HEAD` ancestry.

## One-command preparation

From the repository root:

```bash
bash scripts/run_local_test_suite.sh /absolute/path/to/binance_usdtm_futures_history prepare
```

The launcher selects Python 3.13 or 3.12, creates `.venv-local-test`, installs
the Backtest Engine and the modules used by `configs/integration.example.yaml`,
runs `pip check`, scans the full BTCUSDT 5m file, generates isolated configs,
and executes `modular-backtest validate-data`.

Run the deterministic smoke ladder after preparation:

```bash
bash scripts/run_local_test_suite.sh /absolute/path/to/binance_usdtm_futures_history smoke
```

The smoke ladder performs:

1. full dataset and environment preflight;
2. Backtest Engine data validation;
3. a 14-day smoke run;
4. an identical run in a separate result root;
5. result comparison;
6. a 90-day checkpoint/replay run.

Run the full BTC period only after smoke passes:

```bash
bash scripts/run_local_test_suite.sh /absolute/path/to/binance_usdtm_futures_history full
```

## Generated workspace

All generated state is placed under:

```text
.local-tests/pretest/
```

It contains:

- `pretest-report.json` — machine-readable PASS/WARN/FAIL evidence;
- `configs/smoke.yaml`;
- `configs/smoke_repeat.yaml`;
- `configs/restart_replay.yaml`;
- `configs/full_btc.yaml`;
- `configs/modules.yaml`;
- `commands.sh`;
- separate result roots for each scenario.

The generated configs request only `15m`, `1h`, and `4h`. They intentionally do
not request `1d`, because the supplied download summary does not contain a daily
series. Higher timeframes remain built from completed 5m bars by the engine.

## Blocking checks

The preflight fails on:

- Python other than 3.12 or 3.13;
- checkout not descended from the canonical checkpoint;
- dirty worktree unless `--allow-dirty` is explicitly used;
- less than 10 GiB free space;
- missing or malformed `download_summary.csv`;
- non-ok download status;
- reported or observed 5m gaps;
- duplicate or out-of-order timestamps;
- non-finite or invalid OHLCV rows;
- row-count or timestamp-range disagreement with the summary;
- ambiguous BTCUSDT 5m file discovery;
- missing integration module config.

Ten to twenty GiB free is accepted with a warning; at least twenty GiB is
recommended.

## Accepted warnings

The following do not block local paper/backtest tests, but they do block a
production-performance claim:

- funding is disabled in Backtest Engine v0.1;
- `download_summary.csv` does not prove historical exchange-info lineage;
- historical tick/step/notional changes are not represented by the summary;
- Trading Runner still needs an application-specific live/local market-data
  bootstrap for a continuously running paper service;
- no real exchange account, credentials, or private transport is involved.

## Manual invocation

The preflight can be run without environment installation:

```bash
python3.13 scripts/local_pretest.py \
  --dataset-dir /absolute/path/to/binance_usdtm_futures_history \
  --repo-dir . \
  --run-validation
```

Use `--data-file` when more than one BTCUSDT 5m file exists. `--quick` scans
only a leading CSV sample and is intended for script diagnostics, not final
readiness.
