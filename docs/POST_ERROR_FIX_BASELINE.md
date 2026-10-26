# Post-error-fix baseline — Modular Strategy Core 0.4.2

## Identity

- Remote base branch: `integration/modular-strategy-v0.5.0`
- Remote base commit: `5e03b933207a99940244634f49a756d27888a5c3`
- Previous remote candidate: `362e11b5876fdab82bbc1f5183fc9630dcdc6187` / Core 0.4.1
- Local working branch: `core/hardening-v0.4.1-error-audit`
- Candidate version: `0.4.2`
- Module API: `1.0 draft`
- Python baseline: CPython 3.13.5

## Correctness and quality

- Full repository tests: **297 passed**.
- Statement coverage: **87.90%** (`2826 / 3215`).
- Branch coverage: **71.93%** (`738 / 1026`).
- Combined branch-aware coverage: **84.04%**.
- Component Ruff: pass.
- strict mypy: pass across 25 core source files.
- compileall: pass.
- Bandit: 0 findings.
- detect-secrets: 0 candidates.
- Wheel: `modular_strategy_core-0.4.2-py3-none-any.whl` built.
- Source distribution: `modular_strategy_core-0.4.2.tar.gz` built.
- Twine metadata: pass.
- Wheel-only clean installation: pass.
- Public import and PEP 561 marker: pass.
- Checkpoint save/load/restore from installed wheel: pass.

## Performance baseline

### Event loop

- workload: 20,000 bars × 10 modules = 200,000 module calls;
- runs: 5;
- median: **2.1866 s**;
- throughput: **91,466 calls/s**;
- deterministic run identity: pass.

### Multi-timeframe load

- workload: 30,000 bars × 5 modules, 5m/15m/1h;
- runs: 3;
- median: **2.4467 s**;
- throughput: **61,306 calls/s**;
- peak RSS: **99,296 KiB**;
- checkpoint size: **1,169 bytes**;
- deterministic run identity: pass.

### Checkpoint restore

- modules: 20;
- iterations: 100;
- checkpoint JSON: 10,478 bytes;
- median load: **4.2425 ms**;
- median restore: **0.0469 ms**;
- peak RSS: **33,060 KiB**.

## GitHub status

- Existing Draft PR: #13, open and unmerged.
- Previous remote head: `362e11b5876fdab82bbc1f5183fc9630dcdc6187`.
- Previous 0.4.1 workflows: successful.
- Current 0.4.2 changes: not yet pushed at baseline capture.
- Self-hosted runner labels: not exposed by the available connector; pending confirmation.
