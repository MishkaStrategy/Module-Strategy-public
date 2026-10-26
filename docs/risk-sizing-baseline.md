# Risk & Position Sizing — baseline audit

## Scope and source of truth

- Repository: `mishkacher/Module-Strategy`.
- Required base branch: `integration/modular-strategy-v0.5.0`.
- Base commit at audit start: `5e03b933207a99940244634f49a756d27888a5c3`.
- Module branch at audit start: `modules/risk-sizing`.
- Module baseline commit: `e21afcbaf0f6004fd2cb7870bd84331617bc95fb`.
- Existing module PR: #9, initially targeted at `main` rather than the integration branch.
- Core: Modular Strategy Core `0.4.0`.
- Module API: `1.0` draft.
- Module version: `1.0.0`.
- Baseline state schema: `1`.

## Baseline automated evidence

| Gate | Baseline result |
|---|---|
| GitHub CI | successful on Python 3.12 and 3.13 |
| Existing module tests | 53 passed |
| Module statement coverage | 95% in the original local audit |
| `compileall` | passed |
| Root wheel build | passed |
| Ruff | not configured or executed for this module branch |
| strict mypy | not configured or executed for this module branch |
| standalone module wheel | absent |
| module entry point | absent |
| clean module-wheel install | not possible as a separate package |
| real integration-branch smoke | absent; branch was based on `main` |
| checkpoint/restore | mapping/list round-trip passed, but real core deep-freeze compatibility was not tested |
| benchmark | 100,000 pure calculations in 1.009 s, approximately 99,108 decisions/s; one recorded run |
| peak memory | not measured |

## Baseline architecture

The baseline already separated `RiskCalculator` from `RiskSizingModule`, used `Decimal`, declared `risk.request -> risk.decision`, bounded processed request IDs and returned structured reject reasons. It did not modify the kernel.

The core also contains `FixedFractionalSizer`. That component remains the execution-layer final safeguard. The module is an upstream pre-trade policy decision with instrument, leverage, symbol and portfolio constraints. The overlap is deliberate defence in depth; the downstream contract must pass an explicit approved quantity so the core safeguard never increases it.

## Baseline defects and gaps

| Severity | Finding |
|---|---|
| High | Module PR targeted `main`, not `integration/modular-strategy-v0.5.0`. |
| High | Restore accepted only `list`; real core checkpoint deep-freeze supplies tuple-like sequences. |
| High | Input freshness, expiry, feature schema and symbol consistency were not enforced. |
| High | `setup_id` entered `RiskRequest` but was discarded from `RiskDecision`. |
| High | One module instance could silently switch symbols while retaining request caches. |
| Medium | Unsupported payload values could repeatedly produce the same rejection. |
| Medium | Exact Decimal configuration and float-valued core run identity could disagree. |
| Medium | Direct config construction accepted non-integer integer fields; some maxima accepted zero. |
| Medium | Request metadata was only shallowly immutable. |
| Medium | No typed `RiskDecision.from_mapping` existed for downstream consumers. |
| Medium | No standalone wheel, entry point, PEP 561 marker or clean-wheel smoke existed. |
| Medium | No measured memory, checkpoint size, restore latency or long restart workload existed. |
| Low | Documentation covered ten previous rounds, not the requested 30-round programme. |

## Baseline release decision

The baseline was useful for isolated research but was not ready for integration PR #8. Deep hardening was required before retargeting PR #9 to the integration branch.
