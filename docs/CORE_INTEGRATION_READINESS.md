# Core Integration Readiness

Status: **FROZEN FOR MODULE INTEGRATION**

This checklist is the entry gate for integrating Modular Strategy Core with the
project modules. It does not authorize merge, live execution or package
publication.

## Validated Engine base

The accepted Engine head is `241afbda31073fa020cac4fd9c596152db77a247`
in Draft PR #161. Authoritative run `30073008891` passed on Python 3.12 and
3.13 with 1,210/1,210 tests per interpreter, 88% branch-aware coverage and
byte-identical distributions.

## Accepted freeze evidence

Draft PR #164 evidence head
`686442152bb415f4d6628b2190b590b8acf9c555`, authoritative run
`30073884923`:

- 981 Core/root + 84 Session Range + 80 Market Regime + 70 FVG;
- 1,215/1,215 tests per Python 3.12 and 3.13;
- failures/errors/skips: 0/0/0;
- branch-aware coverage: 88%;
- source, installed wheel and installed normalized-sdist contract snapshot
  verification passed;
- wheel `5209eaa2628b4f89cd26daf6fdb6b20c730c7c9bcaf9a285e5f727f087b0cd99`;
- normalized sdist
  `781debf6e3f05f2687c8c3f7ba963f38b0ba17442e0e729c6a7e20dd2b63408d`;
- Python 3.12 and 3.13 distributions are byte-identical.

## Required Core gates

- [x] Engine constructor and factory boundaries fail before module lifecycle or
      persistence side effects.
- [x] Canonical candle, timeframe, instrument, session and registry boundaries
      are enforced.
- [x] Public API snapshot matches `docs/contracts/core-v0.4.2.json` in the
      authoritative Python 3.12/3.13 matrix.
- [x] Contract snapshot passes from source, wheel and normalized sdist.
- [x] Session Range, Market Regime and FVG contract suites pass on the freeze
      head.
- [x] Cross-version wheel and normalized-sdist hashes match on the freeze head.
- [x] Event replay reports a valid contiguous stream in the freeze matrix.
- [x] Module checkpoint state captures and restores deterministically in the
      freeze matrix.
- [x] Runtime module failure quarantines dependent modules without stopping an
      independent module when strict module errors are disabled.
- [x] Same-bar feature propagation follows the dependency plan.
- [x] Core handoff and issue #103 record the accepted freeze evidence SHA.

## External integration gates

These are not Core implementation responsibilities, but integration must not
start without an owner and contract for each gate.

### Router to Paper Execution

Paper Execution must consume Router `ExecutableTradePlan` or
`DownstreamPlanEnvelope` canonical evidence. It must not recalculate risk,
quantity, side, protection, target allocation, sequencing or OCC/hash material.

Required Paper Execution adapter scenarios:

- open, add, reduce and close;
- market, limit and trigger orders;
- cancellation-before-replace and protection-before-entry;
- stale portfolio/content/version rejection;
- duplicate/missing precondition rejection;
- idempotent replay of identical plan evidence;
- rejection of identical `plan_id` with different content;
- canonical JSON and payload-digest verification;
- preservation of Router string OCC values.

### Trading Runner

The runner must orchestrate lifecycle only. It may not compose a trade plan,
change strategy rules or reinterpret execution intent. Restart must recover
checkpoint, pending orders, open position, idempotency evidence and
reconciliation state before accepting new plans.

### Research components

Research Runner and Results Aggregator may consume Core manifests, hashes,
events, trades, metrics and checkpoints but may not alter execution or strategy
contracts.

## Freeze rule

Planned Core development is stopped. Further Core changes require a reproduced
integration/recovery defect, a focused regression, documented ownership and a
complete replacement artifact matrix.

## Next action

Wait for Paper Execution readiness. When it is ready, assemble the integration
head against the frozen snapshot and run the Router → Paper Execution → Core →
Trading Runner scenarios. Paper trading, the five-day run, venue deployment and
live-trading decisions are later gates.
