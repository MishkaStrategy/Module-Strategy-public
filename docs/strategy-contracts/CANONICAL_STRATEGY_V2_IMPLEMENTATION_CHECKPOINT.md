# Canonical Strategy Variant 2 implementation checkpoint

Status: `PENDING_EXACT_HEAD_VALIDATION`

## Identity

- Issue: `#312`
- Draft PR: `#316`
- Branch: `strategy/canonical-fvg-rt-frozen-contracts`
- Base: `c3eea068ff1d04c569ac28807102fc4f7918d610`
- Composition hash: `51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9`
- Candidate ID: `strategy-candidate-51659c6ed582f2843357bd33`

## Implemented

- owner-approved Final 15m, Frozen 1H and Frozen 4H versioned contracts;
- complete `strategy-composition-v1` candidate;
- deterministic contract-bound order-plan adapter;
- fail-closed checkpoint and restart identity fence;
- one-limit/one-position arbitration with priority Final 15m, Frozen 1H, Frozen 4H;
- expiry-before-signal, higher-priority pending-order preemption and no open-position preemption;
- official Binance USD-M REST kline source;
- checksum-verified Binance Public Data archive fallback after exact HTTP 451;
- fixed 721,597-row BTCUSDT 5m range;
- deterministic component and combined ledger generation;
- exact repeated-run diff;
- sanitized evidence artifact contract;
- locked numerical environment: NumPy 2.3.3, Pandas 2.3.3, PyArrow 25.0.0.

## Fail-closed evidence boundaries

- retained Drive corpus SHA is not promoted to the new run unless exact column parity is performed;
- retained registered-dataset hash is recorded only as retained identity;
- without Drive parity, the new official corpus declares `UNPROVEN_FOR_THIS_RUN` relative to the old registered dataset;
- historical counts 283/91/21/395 remain non-binding references;
- no private exchange API, credentials, production mutation or live trading is used.

## Validation state

This checkpoint is not accepted yet. The authoritative head must pass Fast checks, Strategy Audit Validation, Strategy Factory CI/hardening/soak, Core CI and the full official-corpus backtest workflow. GitHub-hosted actionlint, runner-routing and Test Architecture jobs have repeatedly failed before checkout with no steps or logs; equivalent gates are mirrored inside the self-hosted quantitative workflow, but the hosted outage remains recorded rather than treated as green.
