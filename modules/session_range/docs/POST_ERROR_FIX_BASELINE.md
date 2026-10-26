# Post-Error-Fix Baseline — Session Range 0.2.0

## Identity

- Remote base: `integration/modular-strategy-v0.5.0`
- Remote base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Local working branch: `local/session-range-0.2.0-rc`
- Code checkpoint: `e82547da83a6177e923564a96855396976c3d773`
- Core: Modular Strategy Core 0.4.0
- Module API: 1.0 draft contract exposed by Core 0.4.0
- Candidate: modular-session-range 0.2.0
- Python executed: CPython 3.13.5; static target Python 3.12

## Quality baseline

| Gate | Result |
|---|---|
| Tests | 139 passed |
| Coverage | 97% (`808` statements, `27` missed) |
| Ruff lint | pass |
| Ruff format | pass |
| strict mypy | pass |
| compileall | pass |
| wheel | pass |
| source distribution | pass |
| clean install | pass |
| clean `pip check` | pass |
| public import | pass |
| entry point | pass |
| processing smoke | pass |
| checkpoint/restore smoke | pass |
| deterministic replay | pass |
| no-look-ahead/future invariance | pass |

## Performance baseline after fixes

Interleaved same-environment comparison against 0.1.0:

- 10,000 events: candidate 42,166/s vs baseline 40,474/s (**+4.18%**).
- 50,000 events: candidate 42,667/s vs baseline 40,883/s (**+4.36%**).
- 250,000 events: 43,975/s, three-run median.
- 1,000,000 events: 42,202/s in an isolated stress run.
- stress checkpoint: 1,157 bytes.
- stress restore: 108.8 microseconds.
- traced 10k peak median: candidate 63,276 bytes, baseline 62,927 bytes; bounded difference +0.55%.

Raw evidence is stored in `artifacts/benchmark_results.json`.

## CI status

- Existing PR #19 CI failures belong to its interrupted transport state and are not evidence for this candidate.
- Self-hosted snapshot job remained queued, indicating an unavailable or mismatched runner.
- GitHub-hosted snapshot failed before job steps, consistent with the current hosted Actions availability/limit issue.
- The candidate is not declared CI-green until a clean branch runs the intended checks.
