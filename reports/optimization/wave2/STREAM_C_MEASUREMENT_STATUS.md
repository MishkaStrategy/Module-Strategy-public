# Stream C Measurement Status

## Verdict

`ACCEPTED — REMEDIATION COMPLETE`

Stream C established a deterministic no-network SLO harness, reproduced an unbounded
persistent-state breach, introduced a minimal fail-closed SQLite capacity contract and
proved semantic parity with identical full before/after workloads.

This is local/self-hosted evidence, not a production transport claim.

## Evidence identity

- Branch: `optimization/wave2-routing-signal-slo`
- Draft PR: `#309`
- Base: `integration/optimization-wave-1`
- Base SHA at stream creation: `041ee4466232feab448daa377fb33d7898d50fd9`
- Stream B contract: `wave2c-routing-signal-v2`
- Benchmark schema: `2`
- Generator: `tests/signal_bot/wave2_slo_benchmark.py`

## Baseline

- Head: `57539351159a9d99ca90c47dc92785b69e2ad9d6`
- Core CI: `30745424109`
- Artifact: `8832876395`
- Artifact SHA-256:
  `124b489a97c2c12fac5ffc0616e419943a01229b0efdd123d8b6f02e0a9ede07`
- JSON SHA-256:
  `d3e24ae9b9afe6d626d107b12821de4b79fabdb9f925d871053088c84b2e0f8e`
- 102 records, 34 groups, three repeats
- semantic SLO: PASS

Confirmed breach:

- recovery fetch bounded to 100;
- pending/retry admission unbounded;
- total retained identity admission unbounded;
- 100,000 identities occupied 111,746,672 bytes;
- approximately 1,117 bytes per retained identity.

## Remediation

`SQLiteStateStore` now enforces:

- total identity capacity: 100,000;
- pending/retry capacity: 10,000;
- `StateCapacityError` fail-closed overflow;
- durable O(1) state counters;
- restart reconciliation;
- cross-connection `BEGIN IMMEDIATE`;
- duplicate suppression before capacity rejection;
- no automatic terminal evidence deletion.

## After profile

- Head: `ffadc5829bcf747e842c7a97adfc0bbf33083dd7`
- Core CI: `30746786825`
- Artifact: `8833297064`
- Artifact SHA-256:
  `d619172a9430c5dbf5535582f8be67913752fc97c0713df382242ad509cd25a6`
- JSON SHA-256:
  `ab818479d5913047ba1b9a23ec18e6184659f97ef43213ed809d2ccaa5fb0765`
- 102 records, 34 groups, three repeats
- semantic SLO: PASS

Headline after results:

| Workload | Throughput mean | Max sample p99 |
| --- | ---: | ---: |
| Router steady 100k | 1,196.90/s | 1.298 ms |
| Router burst 10k | 1,077.57/s | 6.477 ms |
| Signal steady 100k | 2,303.84/s | 2.949 ms |
| Signal burst 10k | 2,402.65/s | 2.022 ms |
| Queue saturation 10k | 2,467.60/s | 1.258 ms |
| Restart 1k | 1,554.51/s | 35.190 ms |
| Soak 100k | 4,142.96/s | 1.889 ms |

Restart recovery maximum: 281.811 ms.

Peak RSS: 190,103,552 bytes.

Persisted size at 100,000 identities: 111,849,144 bytes.

Semantic counters:

- duplicate delivery: 0;
- acknowledged loss: 0;
- invalid admission success: 0;
- ordering violations: 0;
- private payload leakage: 0.

## Variance

One Router burst sample and one Signal soak sample were local runner outliers. Router
production code was unchanged, and the remaining samples did not reproduce the same
regression. The conservative workload SLO includes these samples and still passes, with
a variance warning for Router burst.

Slow-consumer throughput is intentionally dominated by the deterministic notifier delay
and must not be interpreted as Telegram production capacity.

## Local workload SLO

- Router steady 100k: >=1,000/s and <=1.5 ms p99: PASS.
- Router burst 10k: >=1,000/s mean and <=7.0 ms p99: PASS with variance warning.
- Signal steady 100k: >=1,900/s and <=3.0 ms p99: PASS.
- Signal burst 10k: >=1,800/s and <=2.5 ms p99: PASS.
- Restart 1,000: <=350 ms: PASS.
- Peak RSS <=200 MiB: PASS.
- Persisted 100k <=120 MiB: PASS.
- total/pending overflow fail closed: PASS.
- all semantic zero-tolerance counters: PASS.

No additional production optimization is authorized.

## Validation

Completed evidence includes:

- compileall;
- Ruff lint and format;
- strict MyPy;
- 87 Signal Bot tests on Python 3.12 after remediation;
- Python 3.12 and 3.13 Core CI;
- v0.6 executable acceptance;
- wheel verification;
- smoke, full baseline and full after-profile;
- restart, retry, dedup, corruption and cross-connection capacity tests.

The one-shot full test was deleted in commit
`15265ba9ccf70d1cddac9fe5fe9c43d6f9f8cdc2`.

## Handoff

- Before/after evidence:
  `reports/optimization/wave2/stream-c-before-after.json`
- Stream B v2 contract:
  `reports/optimization/wave2/stream-c-handoff-b.json`
- Full topology and contract:
  `docs/optimization/wave2/STREAM_C_ROUTING_SIGNAL.md`

Final exact-head Actions remain authoritative; queued or in-progress runs are not
counted as success.
