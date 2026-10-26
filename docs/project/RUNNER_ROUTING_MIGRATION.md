# Runner Routing Migration

## Status

Accepted and authoritative.

## Current rule

GitHub Actions routing uses two movable workload-role labels:

- `[self-hosted, fast]` for every job that is not an actual historical
  trading-strategy backtest;
- `[self-hosted, backtest]` only for historical strategy backtests and research
  that directly executes backtests.

The labels are not tied to a VDS, Mac, operating system, architecture, hostname
or physical machine. The user may move either label between registered runners
without changing workflow source.

## Explicitly revoked statements

The following old statements are superseded and must never be restored from
ChatGPT memory, archived chats, old prompts, PR descriptions, comments,
checkpoints or historical commits:

- fast jobs must run on VDS/Linux/X64;
- heavy jobs must run on Mac/macOS/ARM64;
- jobs exceeding about 20 minutes must move to `backtest`;
- CPU-, memory-, disk- or data-intensive jobs must move to `backtest`;
- `@pytest.mark.slow` selects or implies a backtest runner;
- words such as heavy, audit, stress, recovery, soak or performance determine
  runner routing;
- a runner label identifies one permanent physical machine.

## Classification examples

Non-backtest work remains `fast` regardless of cost or duration: linting,
typing, unit and integration tests, full-repository suites, security scans,
packaging, coverage, recovery, replay, deterministic stress, synthetic
benchmarks, transport and artifact jobs.

Backtest work uses `backtest`: historical strategy simulations, backtest-driven
parameter search, walk-forward analysis, and bootstrap or Monte Carlo over
backtest trades, equity curves or outcomes.

## Source precedence

When sources conflict, use this order:

1. the user's latest explicit instruction;
2. `AGENTS.md` and `.github/WORKFLOW_RULES.md` on the active authoritative branch;
3. current workflow source;
4. current module documentation;
5. PR descriptions and comments;
6. archived chats, prompts and remembered context.

Archived information is historical evidence only. It is not permission to
restore machine-bound routing.

## Load distribution

Every interchangeable ordinary runner may carry the same `fast` label. Jobs
request only `[self-hosted, fast]`, allowing GitHub to assign them to any
matching online and idle runner. Exact round-robin distribution is not
guaranteed. Independent work should be expressed as independent jobs, without
unnecessary `max-parallel: 1` or repository-wide serialization.
