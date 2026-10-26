# Instructions for AI Agents

Before changing this repository, inspect its existing architecture,
configuration, and conventions. Do not create a parallel implementation of an
existing component, and preserve backward compatibility unless a breaking
change is explicitly requested.

## GitHub Actions

Before creating or editing anything in `.github/workflows/`, read and follow
`.github/WORKFLOW_RULES.md` and `.github/RUNNER_LABELS.md`.

The repository may use both approved self-hosted capability runners and approved
GitHub-hosted runners.

Self-hosted selectors:

```yaml
runs-on: [self-hosted, fast]
runs-on: [self-hosted, docker]
runs-on: [self-hosted, macos]
runs-on: [self-hosted, backtester]
```

GitHub-hosted selectors:

```yaml
runs-on: ubuntu-latest
runs-on: macos-latest
```

Workload roles:

- `fast` — ordinary lint, typing, unit, package, security and quick integration;
- `docker` — jobs that genuinely require Docker or containers;
- `macos` — jobs that genuinely require macOS;
- `backtester` — historical strategy backtests, backtest-derived quantitative
  research, extended quantitative audits and long quantitative soaks.

Use `[self-hosted, fast]` or `ubuntu-latest` for ordinary fast work. Use
`[self-hosted, docker]` or `ubuntu-latest` for actual Docker/container work. Use
`[self-hosted, macos]` or `macos-latest` for genuine macOS work. Historical
quantitative workloads remain on `[self-hosted, backtester]` unless the runner
policy is explicitly changed again.

GitHub-hosted jobs consume organization Actions minutes. Select them
intentionally for capacity, availability or isolation.

Never use bare `self-hosted`, legacy `backtest`, `windows-latest`, unapproved
hosted selectors, physical runner names, hostnames, provider labels, or
OS/architecture labels used merely to pick a machine. Split incompatible
environment requirements into separate jobs instead of inventing combined
selectors.

A job belongs to `backtester` only when it executes a historical strategy
simulation, backtest-derived research, an extended quantitative audit, or a long
quantitative soak. A slow non-quantitative test remains `fast` unless it actually
requires Docker or macOS.

Python jobs must use the repository reusable action after checkout:

```yaml
- uses: ./.github/actions/python-runner-preflight
  with:
    role: fast
    python-version: "3.12"
```

The `role` describes the workload rather than runner ownership. The action
performs fail-closed resource and capability checks and provisions an exact
Python 3.12 or 3.13 environment using pinned `uv`. Do not introduce a parallel
provisioning scheme without an explicit documented architecture exception.

To let GitHub distribute work across matching self-hosted and GitHub-hosted
runners, keep independent checks in independent jobs where practical and do not
add repository-wide serialization or `max-parallel: 1` unless correctness
requires it.

`@pytest.mark.slow` controls test selection only. It never selects a runner.

## Python

- Use Python 3.12 or newer.
- Add type hints to new or changed Python interfaces.
- Use pytest for tests, Ruff for linting and formatting, and strict MyPy where
  configured by the project.
- Do not hide required quality gates with `continue-on-error`.
- Mark genuinely long-running tests with `@pytest.mark.slow` when useful, but
  route them according to their actual workload capability.

## Trading and backtesting work

The repository remains paper-only unless the user separately authorizes a
future live design. Do not add private exchange access, credentials, order
placement, real portfolio mutation, a live feature flag, deployment or a
production/live readiness claim.

If one job mixes backtest and non-backtest steps, route the whole mixed job to
`backtester`; preferably split independent lint, typing, security and packaging
checks into separate `fast` jobs so free runners can process them in parallel.
