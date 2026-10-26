# Canonical runner capability policy

Every GitHub Actions job in this repository must use either one approved
self-hosted workload capability or one approved GitHub-hosted selector. Runner
routing describes workload requirements, never a physical machine identity.

## Approved selectors

### Self-hosted capability pool

```yaml
runs-on: [self-hosted, fast]
runs-on: [self-hosted, docker]
runs-on: [self-hosted, macos]
runs-on: [self-hosted, backtester]
```

- `fast` — lint, formatting, typing, unit tests, contract tests, package builds,
  quick integration, security, dependency, SBOM and ordinary recovery checks.
- `docker` — only jobs that actually require Docker or containers.
- `macos` — only jobs that genuinely require macOS behavior or tooling.
- `backtester` — historical strategy backtests, backtest-derived quantitative
  research, extended quantitative audits and long quantitative soaks.

The `market-data-external-corpus-drive-acceptance.yml` workflow is an approved
extended historical audit for the `backtester` pool. It scans the complete
615,395,637-byte, 50-file baseline corpus and verifies content hashes, schemas,
time bounds, continuity and duplicate invariants before producing sanitized
evidence. This routing is based on the historical workload and its data
capability, not on a runner hostname or physical machine identity.

The `market-data-external-corpus-registration-ci.yml` workflow is the approved
follow-on registration audit. It re-verifies the same corpus, preserves every
source Parquet file as immutable local raw lineage, produces normalized monthly
Kline partitions, registers ten instrument datasets through the canonical
`LocalStorage` and `SQLiteDatasetRegistry` contracts, performs deep registered-
dataset integrity checks and uploads only sanitized hash evidence. Its temporary
storage is removed after the job and no raw or normalized market bytes are
uploaded to GitHub.

### GitHub-hosted pool

```yaml
runs-on: ubuntu-latest
runs-on: macos-latest
```

- `ubuntu-latest` may execute ordinary `fast` work and jobs that require the
  Docker tooling available on GitHub-hosted Ubuntu.
- `macos-latest` may execute genuinely macOS-specific work.
- GitHub-hosted jobs consume organization Actions minutes and should be used
  intentionally for capacity, availability or isolation.
- Historical strategy simulation, backtest-derived research, extended
  quantitative audits and long quantitative soaks remain on
  `[self-hosted, backtester]`.

## Forbidden routing

The following are rejected fail-closed:

- `windows-latest` or any unapproved GitHub-hosted selector;
- `runs-on: self-hosted`;
- `runs-on: [self-hosted]`;
- the legacy `backtest` label;
- runner display names, hostnames or physical-machine identity;
- Linux, architecture, VDS or provider labels used only to select a machine;
- unapproved combinations of capability labels.

A workflow requiring two incompatible environments must split the work into
separate jobs rather than inventing a combined selector.

## Mandatory preflight and Python provisioning

Python jobs must check out the repository and invoke:

```yaml
- uses: ./.github/actions/python-runner-preflight
  with:
    role: fast
    python-version: "3.12"
```

The `role` describes the workload and must be one of `fast`, `docker`, `macos`
or `backtester`, regardless of whether the selected runner is self-hosted or
GitHub-hosted.

The reusable action:

1. checks Git and required system utilities;
2. verifies writable workspace and temporary directories;
3. checks role-specific disk-pressure thresholds and enforces a RAM minimum only for `backtester`;
4. verifies Docker daemon access for the `docker` role;
5. verifies Darwin for the `macos` role;
6. provisions exact Python 3.12 or 3.13 using `uv` 0.11.31;
7. creates a run-scoped isolated virtual environment;
8. fails before project installation when the runner does not satisfy an enforced role requirement.

### RAM policy

RAM is observed and reported for diagnostics on every supported role, but it is
not a preflight scheduling gate for ordinary workloads.

| Capability | RAM requirement |
| --- | --- |
| `fast` | no minimum |
| `docker` | no minimum |
| `macos` | no minimum |
| `backtester` | >= 8 GiB |

Low-memory `fast`, `docker`, and `macos` hosts may therefore continue and rely
on the operating system and configured swap policy. The `backtester` role keeps
the 8 GiB fail-closed minimum because quantitative backtests and extended
research workloads have a materially larger memory footprint.

### Disk-pressure tiers

Runner disk capacity is classified before project installation. The existing
critical minima remain fail-closed; the high-water tier is an earlier warning
that does not weaken the minimum.

| Capability | Healthy | High-water warning | Critical failure |
| --- | --- | --- | --- |
| `fast` | >= 2 GiB free | >= 1 GiB and < 2 GiB | < 1 GiB |
| `docker` | >= 15 GiB free | >= 10 GiB and < 15 GiB | < 10 GiB |
| `macos` | >= 12 GiB free | >= 8 GiB and < 12 GiB | < 8 GiB |
| `backtester` | >= 30 GiB free | >= 20 GiB and < 30 GiB | < 20 GiB |

Preflight emits a structured key/value line such as
`runner preflight capacity: role=fast state=high-water ...` and includes
`disk_pressure`, `high_water_gib` and `critical_gib` in its final success line.
This output is intended to be consumable by unified observability without
binding policy to a physical runner hostname.

The `backtester` role performs its existing bounded scratch/cache reclamation
before capacity classification. Critical pressure still stops the job before
project dependencies, test data or generated evidence are written.

Project workflows must not mix `actions/setup-python`, arbitrary host Python,
unpinned `uv`, and ad-hoc virtual-environment schemes without an approved,
documented exception.
