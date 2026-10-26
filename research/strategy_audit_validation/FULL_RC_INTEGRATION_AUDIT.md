# Independent Strategy Auditor full-RC integration audit

Status: candidate validation in progress.

## Exact lineage

- source issue: `#250`;
- source PR: `#266`;
- source head: `fe0d7ab77274d043ba228ca7c36ce24ff370953b`;
- isolated candidate: `integration/strategy-auditor-premerge`;
- ordinary source merge: `01dce1fb914e0fa0ef4a2dc4a7e1011b722e4984`;
- full-RC checkpoint before the merge: `afc163526a20aec41e639bac8b4ba3b4bb4b3d63`.

The full-RC branch is not moved until the exact candidate matrix is green.

## Independent ownership

The auditor is a separately packaged local authority. Strategy Factory may provide
an evidence-package reference but does not own the validation policy, signing key,
durable decision chain or admission outcome.

The only outcomes are:

- `APPROVED_FOR_PAPER`;
- `RESEARCH_ONLY`;
- `REJECTED`;
- `UNAVAILABLE`, mapped to Factory `INSUFFICIENT_EVIDENCE`.

There is no live authorization outcome. `AuditDecision` rejects any
`live_trading_authorized=true` value.

## Evidence envelope

A valid immutable package binds:

- Dataset Registry identity, version, manifest, dataset hash and futures funding
  lineage where applicable;
- candidate identity, parameters and canonical configuration hash;
- chronological train, validation, test and one-time isolated holdout windows;
- Results Aggregator package hashes and independent verifier result;
- robustness report identity and pass state;
- paper-only execution assumptions with no private exchange access or order
  placement;
- bounded paper-only risk and drawdown policy.

Unavailable required Dataset Registry or Results Aggregator verifiers fail closed to
`UNAVAILABLE/INSUFFICIENT_EVIDENCE`. Unknown but reachable evidence can only remain
`RESEARCH_ONLY`.

## Durable integrity

- evidence references are content addressed and cannot be reused with different
  content;
- decisions are bound to evidence identity and a contiguous append-only record
  chain;
- every durable evidence row and decision is verified before or after public
  operations;
- the process-supplied HMAC key is never persisted;
- every historical HMAC is reverified before accepting new evidence, so an attacker
  cannot hide a forged historical decision by recomputing the public SHA-256 chain;
- exact requests replay the original signed decision across restart and later clocks;
- wrong keys, altered evidence, malformed rows, broken links and recomputed forged
  signatures are rejected.

The candidate also includes a bounded 64-decision restart soak that checks the full
signature sweep, contiguous sequence, chain linkage and first/last replay after
reopening the SQLite store.

## Quality contract

The exact combined candidate must pass:

- Python 3.12 and 3.13 through the shared `[self-hosted, fast]` preflight;
- compileall, Ruff lint/format and strict MyPy;
- at least 95% branch-aware coverage;
- deterministic wheel and sdist builds performed twice;
- clean installation and dependency checks for both wheel and sdist;
- repository Fast, Core, actionlint, runner-routing and environment policies.

Strategy Factory integration remains blocked until its produced evidence package is
expanded and contract-tested against this independent port. No merge to `main`,
release, deployment, private exchange access or live execution is authorized by
this audit.
