# RC2 Benchmark Report

Environment measurements are comparative local evidence, not a latency SLA.

## Timed runs

- 5 × 5,000 bars: median `4,276.43 bars/s`; min `4,105.23`; max `4,337.09`.
- 100,000 continuous bars: `4,038.57 bars/s`, `24.76 s`.
- checkpoint restore digest equality: passed.
- 1,000-bar traced sample peak: approximately `0.82 MB`.

## Extended load

1,000,000 bars were processed in ten 100,000-bar chunks with nine checkpoint/restores:

- weighted throughput: `4,026.32 bars/s`;
- chunk min/median/max: `3,928.02 / 4,024.72 / 4,152.51 bars/s`;
- Feature publications: `1,299,948`;
- checkpoint size: `315,870–316,092 bytes`;
- final retained state: 128 pivots, 127 waves, 3 hypotheses;
- first chunk digest equals an independent continuous 100,000-bar run.

The RC2 hardening adds stricter DTO/checkpoint validation and full Module API view construction. Median 5k throughput is modestly lower than the earlier RC1 local candidate but remains materially above the original remote baseline and state growth stays bounded.
