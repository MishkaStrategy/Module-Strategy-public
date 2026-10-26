# Changelog

## 0.2.1 — correctness hardening

### Incremental continuation — 2026-07-22

- reject future-dated positions, empty or malformed market views, unrepresentable candle close times and oversized event indices before state mutation;
- prevent entry-bar OHLC values that may predate the fill from activating partial exits or trailing;
- preserve out-of-order and conflicting-duplicate rejection priority;
- reject impossible R-dependent, time-travel and oversized checkpoints transactionally;
- expose confirmed partial stages as typed lifecycle status without repeating close requests;
- keep package, entry-point and historical direct imports on one hardened public implementation;
- enforce finite float transport for Core DTOs and strict-JSON-safe diagnostics;
- bound untrusted Decimal configuration and checkpoint inputs independently from valid wider runtime-context serialization;
- make Decimal fingerprints, runtime arithmetic, status formatting and restore independent of ambient context precision;
- preserve checkpoint field names in Decimal validation errors;
- validate partial-fraction totals exactly across the full supported exponent range;
- bound holding horizon, partial-rule count and counter domains, including infinite iterables;
- synchronize machine-readable manifest limits with runtime validation;
- route fast Python 3.12/3.13 quality gates to the VDS and heavy release/security/packaging/benchmark gates to the Mac Runner;
- isolate Position Exit entry-point tests from unrelated broken third-party module entry points;
- use PEP 604 union types in `isinstance` checks to satisfy Ruff UP038 without changing validation semantics;
- add focused regression coverage for the incremental hardening surface;
- document the monotonic deterministic `MarketView.index` requirement across checkpoint restore for cancellation retry continuity;
- reject configuration objects outside the declared `int | float | str | Decimal` numeric contract before invoking user-defined string conversion;
- replace the absolute partial-close epsilon with a tolerance derived from Core float transport ULPs so valid tiny positions are not silently marked complete;
- reject noncanonical restored UTC timestamps and checkpoints whose stored initial stop cannot define positive initial risk while preserving legacy schema/fingerprint compatibility;
- continue an already activated partial-close retry across a data-gap bar without consuming corrupt OHLC extremes, while retaining `exit_pending` and time-exit priority;
- prevent Decimal protective stops from being emitted when float transport collapses them onto or through the completed-bar market price;
- reject oversized configuration and checkpoint Decimal strings before invoking the Decimal parser;
- preserve a positive residual when a near-full partial request would otherwise round to the full factual position quantity during float transport;
- require an observed factual quantity decrease before ULP tolerance may confirm a partial stage;
- fail closed without completing the stage when a subnormal position cannot encode a positive partial-close request;
- verify runtime commit `75d2688840e003925d699555aa28739d9713bad7` with 288 module scenarios, 92% branch-aware coverage, Ruff and strict mypy in an isolated Mac hardening run;
- retain the prior complete remote release baseline at commit `599ca262e83ef9953f48550b4bb12b21b47e84e9`: Python 3.12/3.13 fast gates, 268 module tests, 244 repository tests, 267 installed-sdist tests, security/SBOM, packaging, clean-install, replay and benchmark gates passed.

### Original 0.2.1 hardening baseline

- preserve unknown initial risk when a stop first appears after attachment;
- make gap handling visible and prevent gap candles from contaminating trailing extrema;
- expand deterministic duplicate identity to all consumed position, exit and cleanup inputs;
- validate malformed DTOs before cursor or runtime mutation;
- enforce semantic and bounded checkpoint invariants;
- retry activated breakeven and trailing protection after unconfirmed adapter updates;
- prevent pending partial retries from closing scale-in quantity when reset is disabled;
- make stop-action reason codes identify the component that selected the final price;
- reject duplicate pending order IDs and prune stale cleanup retry state;
- preserve exact Decimal configuration identity without float collapse;
- restore and exceed the prior no-op benchmark through safe validation and close-time hot paths;
- include reproducibility fixtures in the source distribution;
- include audit/replay tools in sdist and exclude the repository-only workflow contract from the standalone package suite.

## 0.2.0 — local release candidate

- moved Position & Exit Management into an independent typed package;
- added `modular_strategy.modules` entry point and clean-wheel installation contract;
- added deterministic point-in-time `position_exit_management.status` feature;
- added typed reason codes and management actions;
- added canonical Decimal configuration identity and config fingerprint;
- upgraded checkpoint schema to version 2 with transactional restore;
- added exact/conflicting duplicate and out-of-order detection;
- added `(symbol, timeframe)` stream binding;
- added bounded and rate-limited orphan-order cleanup;
- retained residual-only partial close retry and initial-quantity stage semantics;
- added dual-Core compatibility matrix for integration Core 0.4.0 and hardening Core 0.4.1 candidate;
- added branch coverage, property tests, Bandit, wheel and clean-install gates;
- added reproducible benchmark and load/restart evidence;
- added an early stable-position no-op path without bypassing future-time, scale-in, duplicate, checkpoint or action semantics;
- expanded local regression coverage to 123 passing scenarios;
- strengthened Ruff policy with import, bugbear and pyupgrade checks;
- verified local wheels, clean installation, entry-point discovery and Core 0.4.0/0.4.1 compatibility.

## 0.1.0 — historical baseline

- time exit with retry;
- sequential partial exits;
- breakeven and R/percent trailing;
- checkpoint/restore;
- duplicate bar guard;
- orphan-order cleanup;
- 103 historical test scenarios.
