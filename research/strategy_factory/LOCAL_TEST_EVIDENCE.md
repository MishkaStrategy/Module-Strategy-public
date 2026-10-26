# Verification evidence

The materialized checkpoint is verified on self-hosted runners against the exact repository dependency surface:

- Modular Core `0.4.2`;
- Research Runner `0.3.0`;
- Results Aggregator `0.1.0.dev0`;
- Risk Request Composer `0.1.0`;
- Strategy Factory `0.1.0`.

The explicit Research Runner artifact-inventory and Results Aggregator Core 0.4.2 compatibility regressions are included in this Draft PR.

## Permanent quality CI

Run `30367563503` passed on Python 3.12 and 3.13:

- compileall;
- Ruff;
- strict MyPy for Strategy Factory and Risk Request Composer;
- Strategy Factory: 589 passed, no failures/errors/skips;
- Risk Request Composer: 6 passed, no failures/errors/skips;
- acceptance scenarios: 25/25 passed;
- branch-aware coverage: 91%;
- Bandit and tracked-source safety scan;
- six local wheel builds and clean isolated wheel smoke.

## Hardening evidence

Run `30367564002` passed:

- 41 fault/corruption/storage/control tests;
- ten iterations of four restart/resume/idempotency scenarios;
- bounded canonicalization, artifact persistence, idempotency and memory checks;
- five paper-only and final-audit fail-closed tests.

Hardening artifact digest:

`sha256:f722b0aec0f38a2e61ec41fb68ef76fa3ce72c57d569d52cb73530a1712bbc56`

## Extended restart soak

Run `30367810860` passed:

- 100 independent Python processes;
- four crash/restart/idempotency checks per process;
- 400/400 checks passed;
- deterministic hash seeds 1 through 100;
- elapsed time 79.718079167 seconds, below the 1,800-second bound.

Extended-soak artifact digest:

`sha256:682fff620fc8474d4851308128cbf6ced3a84567d4c5f046dc955a6dd0fe8460`

This does not replace staging evidence under the intended deployment topology.

The real Research Runner → Backtest Engine → Results Aggregator path reaches `REGISTERED`; identical replay reaches `DUPLICATE`. No live exchange, credentials or live execution were used.
