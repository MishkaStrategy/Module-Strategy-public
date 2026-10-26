# Canonical strategy contract recovery audit

## Identity

- Repository: `MishkaStrategy/Module-Strategy`
- Issue: `#312`
- Base branch: `optimization/wave2-strategy-governance-slo`
- Base exact head: `c3eea068ff1d04c569ac28807102fc4f7918d610`
- Work branch: `strategy/canonical-fvg-rt-frozen-contracts`
- Composition name: `FVG-RT Final 15m + Frozen 1H Short + Frozen 4H Short`
- Recovery verdict: `PARTIAL RECOVERY — FAIL CLOSED`

This workstream records only rules with recoverable provenance. It does not infer a rule from a module name, substitute PR #298, or declare the composition canonical, executable, approvable, paper-ready, or live-ready.

## Source priority

The recovery used this priority:

1. exact executable formulas and machine-readable specifications;
2. independently audited strategy checkpoint artifacts;
3. project-approved strategy decisions retained in project context;
4. reports only as control evidence, never as a replacement for a missing formula.

Recovered source identities:

- `FVG_RT_V7_TRANSFER_PROMPT.md`;
- `strategy_spec_v7_checkpoint.json`;
- `FVG_RT_V7_FINAL_AUDIT.md`;
- `strategy_spec_v5.json` and the v6 checkpoint inheritance rule;
- the project-approved Final-15m quality/regime decision;
- the project-approved Frozen 1H Short decision;
- Wave 2F contract registry and canonical gap asset at the base head.

The old v7 reproducibility controls are retained as provenance, not as proof for the newer combined composition:

- trade ledger SHA-256: `4240cc827fdceb7d2c47ba6cb8af25f1d0f0179f987739df3a05b13c88001e0c`;
- audit JSON SHA-256: `52a329448abe89e0e6935a29120de4349a8371f0aa43c225c41cbe4dc0034525`;
- robustness JSON SHA-256: `cfb584d1293264309c1ea77009c2358d99bfa91690d1b9169c578aa192005888`.

These controls belong to the earlier v7 research package and unidentified 2017–2026 source series. They must not be presented as a reproduction of the final combined strategy on official Binance USDT-M futures data.

## Combined champion control inventory

The approved combined champion is expected to contain exactly 395 trades:

- Final 15m: 283 trades;
- Frozen 1H Short: 91 trades;
- Frozen 4H Short: 21 trades.

These counts are acceptance controls only. The exact 395-trade ledger and its digest are not currently resident in the repository, so the combined composition cannot yet be reproduced or declared complete.

## Recovered contract: FVG-RT Final 15m

The complete v7 signal, portfolio, execution, cost, gap-safety, and no-progress specification was recovered. The current project decision adds:

- `impulse_efficiency >= 0.45` for every Long, Short-A, and Short-B setup;
- Long regime: last completed 4h close must be above completed 4h EMA200;
- Short regime: last completed 1d close must be below completed 1d EMA200;
- EMA200 must be warmed up and only completed HTF candles may be consumed.

The recovered contract is still non-executable in this repository because the following remain absent:

- a versioned runtime package/module identity for the complete Final-15m strategy;
- an executable setup adapter in the trusted strategy chain;
- a repository-resident control ledger for the Final quality/regime additions;
- exact integration binding from analytical FVG features to executable orders;
- approved cross-component composition ordering with Frozen 1H/4H.

Therefore the recovered asset is evidence and specification only. It cannot set `contract_complete=true`.

## Recovered contract: Frozen 1H Short

The following approved rules were recovered:

- bearish 1h FVG;
- FVG size `0.08–1.25 ATR`;
- middle candle body at least `0.40 ATR`, bearish;
- middle-candle volume at least `0.85 × SMA20`;
- impulse efficiency at least `0.45`;
- no RSI filter;
- completed 1h close below EMA50 with EMA50 falling;
- last completed 4h close below completed 4h EMA200;
- directional short location over the last eight completed 1h candles at least `0.75`;
- ATR/close at most `2.5%`;
- bearish-zone entry `C.high + 0.75 × (A.low − C.high)`;
- stop at the maximum of the last five completed 1h highs plus `0.05 ATR`;
- maximum stop distance `4%`;
- target `6R`;
- limit TTL `4h`;
- maximum hold `12h`;
- no-progress check after `6h`, requiring MFE of at least `0.50R`.

The contract is still partial. Missing items include exact FVG geometry/equality binding, the short-location formula and zero-range behavior, EMA50 falling lookback convention, first eligible execution bar, intrabar/no-progress priority, cost inheritance, runtime package/version, feature identities, checkpoint schema, composition priority, and the exact 91-trade ledger.

## Missing contract: Frozen 4H Short

Repository history, pull requests, commits, File Library artifacts, and retained project decisions were searched for the exact Frozen 4H Short rules. No complete authoritative specification was found.

The only safe recovered facts are:

- direction: short;
- signal timeframe: 4h;
- it is a distinct component of the named composition;
- its accepted champion contribution is expected to be 21 trades;
- it must use completed HTF candles and remain look-ahead-free;
- it is not the 4h first-retest research baseline from Draft PR #298.

Missing fields include every signal threshold, entry formula, stop, target, TTL, maximum hold, no-progress behavior, cost/sizing overrides, state/restart contract, runtime identity, exact 21-trade ledger, and its priority relative to Final 15m and Frozen 1H.

## PR #298 non-substitution boundary

Draft PR #298 implements a separate BTCUSDT 4h first-retest baseline with proximal-boundary entry, one-zone-width stop, 2R target, and seven-day expiry. Its own report classifies it as an unprofitable research baseline. Issue #312 explicitly prohibits using it as Frozen 4H Short. No rule from #298 is imported into the recovered canonical contract.

## Composition ordering status

Wave 2F contains placeholder order only. No authoritative arbitration contract was found for simultaneous eligible orders across Final 15m, Frozen 1H, and Frozen 4H.

Until approved ordering exists, the composition must not choose among simultaneous signals and must not emit an executable plan. The safe status is:

- state: `INCOMPLETE`;
- canonical: `false`;
- executable: `false`;
- approvable: `false`;
- `load_composition(..., require_canonical=True)`: required to fail.

## Required owner decisions

1. Provide or approve the complete Frozen 4H Short contract.
2. Define deterministic cross-component priority and active-order blocking semantics.
3. Approve runtime module IDs/versions and setup adapters.
4. Provide exact control signal/trade ledgers and digests for 283/91/21 and the combined 395 trades.
5. Confirm whether the Final 15m quality/regime additions supersede v7 under one versioned contract.
6. Approve the remaining Frozen 1H conventions listed above.

## Next executable gate

After all six decisions are authoritative:

1. update the module contract registry with versioned complete contracts;
2. create runtime adapters and positive/negative contract tests;
3. replace the gap composition with a `CANDIDATE` composition;
4. prove stable composition hash and deterministic restart;
5. rerun Wave 2F governance/corruption/replay gates;
6. run the named composition through Research Platform and Backtest only on registered, verified data;
7. compare against the exact 395-trade control ledger before any optimization.

## Safety

No trading rule, production order path, Core DTO, release workflow, deployment, credential, dataset, or live-trading setting is changed by this audit.