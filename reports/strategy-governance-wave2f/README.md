# Strategy Governance Wave 2F evidence

## Verdict

The governance measurement foundation is reproducible and the proposed SLO is met on
the recorded reference environment. No production optimization was performed because
no reproducible SLO breach and no concrete production bottleneck were established.

The requested named trading composition is **not canonical yet**. Exact executable
contracts for all three named strategy slots are missing, and the current trusted
Factory catalog has no executable FVG setup adapter. The committed gap asset is
fail-closed and cannot be approved.

## Reproduction

From the repository root:

```bash
PYTHONPATH=research/strategy_audit_validation/src \
python research/strategy_audit_validation/benchmarks/strategy_governance_slo.py \
  --workload valid --candidates 1000 \
  --database /tmp/wave2f-valid-1000.sqlite3 \
  --output /tmp/wave2f-valid-1000.json
```

Change `--workload` to `repeated`, `mostly-invalid`, `large`, `interrupted`,
`corrupted`, or `concurrent-readers`. The harness does not use network access, exchange
credentials or live trading.

## Headline results

- valid 1,000: 217.15 attempts/s;
  cold p95 12.370 ms; p99
  22.613 ms; RSS delta
  10616832 bytes; artifact
  9182.808 bytes/candidate.
- three valid-1,000 samples: mean 269.00 attempts/s, CV 6.00%,
  one semantic digest.
- interrupted 1,000: restart overhead
  0.762 ms and exact counts.
- repeated 1,000 plus replay: 1999
  duplicate attempts suppressed with one durable identity.
- corruption: `EvidenceTamperedError` detected.
- concurrent readers: 4000 successful
  read operations with zero failures.

## Evidence inventory

- `baseline-evidence.json`: all raw workloads and three variance samples;
- `baseline-summary.json`: aggregate metrics and deterministic digest comparison;
- `slo-proposal.json`: workload-bound thresholds and rationale;
- `contract-gaps.json`: exact canonical composition blockers;
- `evidence-checksums.json`: SHA-256 inventory.

Raw SQLite databases are intentionally not committed.
