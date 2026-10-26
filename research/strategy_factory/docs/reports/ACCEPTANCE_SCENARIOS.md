# Strategy Factory Acceptance Scenarios

This report records the explicit 25-scenario acceptance package requested for the
Automatic Strategy Builder. Every scenario has a stable test in
`tests/test_acceptance_scenarios.py`. The scenarios are deterministic and do not
perform live execution.

| # | Scenario | Expected boundary | Test status |
|---:|---|---|---|
| 1 | New liquid instrument with full history | Dataset accepted for research | PASS |
| 2 | Instrument with short history | Dataset gate rejects insufficient history | PASS |
| 3 | Dataset with gaps | Gap evidence rejects the dataset | PASS |
| 4 | Dataset with duplicates | Duplicate timestamp evidence rejects the dataset | PASS |
| 5 | Spot data supplied for futures research | Market-type mismatch fails closed | PASS |
| 6 | Funding history unavailable | Funding-dependent modules are ineligible | PASS |
| 7 | No strategy meets minimum criteria | Candidate gate returns no approved candidate | PASS |
| 8 | Several close candidates | Explainable Pareto/simplicity tie-break | PASS |
| 9 | Profitable but overfit candidate | Robustness gate rejects instability | PASS |
| 10 | Stable, less profitable candidate | Stable candidate remains eligible | PASS |
| 11 | Insufficient trade count | Candidate gate rejects weak sample size | PASS |
| 12 | Commission-sensitive strategy | Cost-sensitivity gate rejects candidate | PASS |
| 13 | Strategy works only in one year | Temporal robustness gate rejects candidate | PASS |
| 14 | Crash after Research Runner batch submission | Resume does not duplicate budget or batch | PASS |
| 15 | Partial Research Runner result | Nonterminal result cannot be published | PASS |
| 16 | Duplicate completion event | Identical publication is idempotent | PASS |
| 17 | Dataset bytes change during run | Hash drift rejects the dataset | PASS |
| 18 | Corrupted checkpoint | Checkpoint integrity validation fails closed | PASS |
| 19 | Compute budget exhausted | External work is not started | PASS |
| 20 | Cancel followed by resume | Cancelled run cannot silently restart | PASS |
| 21 | Negative untouched holdout | Paper admission remains forbidden | PASS |
| 22 | Final candidate package | Hash-verified paper-only Strategy Package is formed | PASS |
| 23 | Live promotion without permission | Live authorization is structurally impossible | PASS |
| 24 | Identical request with same ID | Exact replay is idempotent | PASS |
| 25 | Same ID with different content | Identity conflict fails closed | PASS |

## Additional promotion evidence

Package-seeded `adapt` and `improve` use the unchanged Strategy Package as a
control arm. The control experiment may activate the baseline configuration;
a structurally changed experiment receives the baseline only as cross-experiment
comparison evidence and never activates a foreign configuration hash.

## Verification command

```bash
PYTHONPATH=/mnt/data/sf_site:src \
  pytest -q tests/test_acceptance_scenarios.py tests/test_seeded_modes.py
```

The release remains paper-only. This acceptance package does not replace the
independent Strategy Audit & Validation component, long-duration staging soak,
performance/memory gates, or production/live authorization.
