# GitHub Actions Runner Rules

These rules are mandatory for every workflow in `.github/workflows/`.

## Approved runner pools

Every job must use exactly one approved selector from either the self-hosted
capability pool or the GitHub-hosted pool.

### Self-hosted capability pool

```yaml
runs-on: [self-hosted, fast]
runs-on: [self-hosted, docker]
runs-on: [self-hosted, macos]
runs-on: [self-hosted, backtester]
```

The labels describe workload capabilities, not machine identity. GitHub may
assign a job to any online and idle runner carrying the selected capability.

### GitHub-hosted pool

```yaml
runs-on: ubuntu-latest
runs-on: macos-latest
```

GitHub-hosted jobs consume organization Actions minutes. Use them intentionally
for capacity, availability or isolation while preserving the workload rules
below.

### `fast`

Use the `fast` workload role for ordinary work that does not genuinely require
macOS or a historical quantitative runner. It may run on either
`[self-hosted, fast]` or `ubuntu-latest`:

- compilation, linting, formatting and strict typing;
- unit, contract, negative and quick integration tests;
- coverage, packaging and clean installation;
- security, dependency, secret, SBOM and license audits;
- ordinary recovery, replay and deterministic checks;
- data validation, conversion, export and artifact verification;
- non-quantitative performance and memory benchmarks.

A large dataset, long duration, high CPU/RAM use, `slow` marker or a job name
containing `heavy` does not by itself change a job from `fast`.

### `docker`

Use the Docker workload role only when the job actually needs a Docker daemon,
container build, container runtime or container-specific integration topology.
The job may use `[self-hosted, docker]` or `ubuntu-latest`. The runner preflight
must verify daemon access and permissions before project installation.

### `macos`

Use the macOS workload role only when the behavior or toolchain is genuinely
macOS-specific. The job may use `[self-hosted, macos]` or `macos-latest`. The
preflight rejects this role on non-Darwin systems.

### `backtester`

Use `[self-hosted, backtester]` for:

- historical strategy simulations;
- backtest-derived parameter optimization and walk-forward analysis;
- bootstrap or Monte Carlo over backtest evidence;
- cost, slippage, funding or execution scenarios requiring rerun of historical
  strategy simulation;
- extended quantitative audits and long quantitative soaks.

Historical quantitative workloads remain on the dedicated self-hosted
`backtester` capability and must not be moved to GitHub-hosted runners without a
separate policy change.

If one job mixes backtesting and non-backtesting steps, route the mixed job to
`backtester`; preferably split independent lint, typing, packaging and security
work into separate `fast` jobs.

## Forbidden selectors

The routing audit must reject:

- `windows-latest` and any unapproved GitHub-hosted selector;
- `runs-on: self-hosted` and `runs-on: [self-hosted]`;
- legacy `backtest`;
- runner names, hostnames, cloud/provider/VDS labels or physical locations;
- OS or architecture labels used only to select a machine;
- unapproved combinations of capability roles.

Do not add PostgreSQL, Redis or similar service labels to `runs-on`. Start those
services in an approved topology, normally `[self-hosted, docker]` or
`ubuntu-latest`, or split the job after an explicit architecture decision.

## Mandatory preflight and Python provisioning

Every Python job must check out the repository and use the local reusable action:

```yaml
- uses: ./.github/actions/python-runner-preflight
  with:
    role: fast
    python-version: "3.12"
```

The `role` describes the workload and is independent of runner ownership.
Supported workload roles are `fast`, `docker`, `macos` and `backtester`.
Supported Python versions are exactly 3.12 and 3.13.

The action performs before project installation:

- Git and system-utility checks;
- writable workspace and temporary-directory checks;
- role-specific disk and RAM thresholds;
- Docker daemon and permission checks for `docker`;
- Darwin verification for `macos`;
- pinned `uv` 0.11.31 installation;
- exact Python installation and a run-scoped isolated virtual environment.

Do not mix this contract with `actions/setup-python`, arbitrary preinstalled
Python selection, unpinned `uv`, or ad-hoc virtual environments unless a focused,
documented exception is approved.

## Distribution and concurrency

To make parallel use of matching self-hosted and GitHub-hosted runners possible:

- express independent checks as separate jobs;
- avoid repository-wide concurrency groups that serialize unrelated work;
- reject `strategy.max-parallel: 1` unless correctness or an external bound
  requires it and the reason is documented;
- use narrowly scoped concurrency groups only to cancel obsolete runs.

## Required workflow behavior

Each workflow must:

- have a clear name and finite `timeout-minutes`;
- use one approved runner selector;
- invoke the fail-closed preflight before Python project installation;
- keep credentials and secrets out of workflow source and logs;
- fail on required installation, lint, format, typing, test or validation errors;
- avoid `continue-on-error` for required quality gates;
- avoid changing repository source unless that is the workflow's explicit
  purpose;
- preserve paper-only behavior and never perform private exchange requests,
  order placement, deployment or live execution.
