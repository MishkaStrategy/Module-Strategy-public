# Acceptance status — package 0.4.0

Status meanings:

- **passed** — executed with deterministic evidence;
- **contract-tested** — parser/planner/fault tests passed without the complete live scenario;
- **blocked** — required environment capability is unavailable;
- **not run** — executable but intentionally deferred until prerequisites exist.

| Scenario | Status | Evidence |
|---|---|---|
| Deterministic request hash | passed | contracts/state tests |
| Same request ID, different content | passed | registry conflict test |
| Identical completed request reuse | passed | pipeline idempotency test |
| Spot/Futures identity separation | passed | capability and mixed-market tests |
| Unknown instrument | passed on Fake; contract-tested on Binance | exact resolver tests |
| Required unsupported stream | passed | pre-download capability rejection |
| Optional unavailable stream | passed | explicit unavailable result |
| Traded/mark/index/premium schema separation | passed | multi-type and codec tests |
| Signed premium-index normalization | passed on live Binance | public smoke plus regression tests |
| Funding event ingestion | passed on Fake; Binance contract-tested | multi-type/Binance tests |
| Open-interest retention gate | passed | required reject and optional clip tests |
| Monthly archive then REST fallback | passed on live Binance | full-history archive/REST acceptance |
| HTTP 429 / Retry-After | passed | network tests |
| Bounded 5xx/timeout/circuit breaker | passed | network fault tests |
| Repeated/non-monotonic/empty API page | passed | Binance paging tests |
| Oversized response | passed | bounded response test |
| Archive checksum mismatch | passed | archive fault test |
| ZIP traversal/member/ratio/corruption | passed | security tests |
| One internal missing interval | passed | validation tests |
| Leading/trailing missing intervals | passed | validation tests |
| Listing/delisting expected absence | passed | lifecycle classification tests |
| Exact duplicate | passed | deduplication evidence test |
| Conflicting duplicate | passed | dataset rejection test |
| Unfinished final candle | passed | provisional rejection test |
| UTC 15m/1h aggregation | passed | reference/streaming aggregation tests |
| Incomplete aggregation group | passed | omitted-group test |
| Multi-type immutable registration | passed | end-to-end fake test |
| Update only new range | passed | immutable v2 operation test |
| Backfill new stream | passed | funding backfill test |
| Backfill overlap rejection | passed | operation conflict test |
| Mid-download failure | passed | checkpoint enters `PAUSED` |
| Resume after restart | passed | raw restore + pending-only acquisition |
| Corrupt checkpoint/resume spec | passed | fail-closed tests |
| Explicit cancel | passed | CLI/run-state tests |
| Concurrent bounded acquisition | passed | determinism/failure tests |
| Parallel same request lock | passed | active lock test |
| Dead-process stale lock | passed | lock recovery test |
| Disk full / permission denied | passed | structured storage fault tests |
| Damaged normalized partition repair | passed | exact replacement + derived rebuild |
| Damaged derived partition local repair | passed | zero-network repair test |
| Damaged metadata local repair | passed | raw exchange-info restoration |
| Kline known-gap repair | passed | exact one-range fetch/merge |
| Mark-price known-gap repair | passed | exact auxiliary gap test |
| Index-price known-gap repair | passed | exact auxiliary gap test |
| Premium-index known-gap repair | passed | exact auxiliary gap test |
| Open-interest known-gap repair | passed | exact auxiliary gap test |
| Wrong-stream repair source | passed | `SOURCE_CONFLICT`, no v2 |
| Multiple damaged streams | passed | staged immutable v2/v3 repair |
| Deep manifest/partition/raw verification | passed | integrity policy tests |
| Raw metadata tamper | passed | deep-validation test |
| Raw contextual ID collision resistance | passed | equal-payload/different-context test |
| CSV exact schema and typed round-trip | passed | streaming storage tests |
| Parquet exact schema and typed round-trip | passed in CI | PyArrow round-trip tests |
| Spool schema/payload/index tamper | passed | spool hardening tests |
| Safe integrity-first export | passed | CLI/export tests |
| Results Registry bridge schema/lineage | passed | exact key set, event-only and mismatch tests |
| Dependency-free AST/source audit | passed | zero findings across production package |
| Seeded deterministic property checks | passed | aggregation/codec/hash/timeframe tests |
| Hypothesis property fuzzing | passed in CI | mandatory property test lane |
| Full Fake CLI vertical slice | passed | discovery/download/inspect/validate/status tests |
| SQLite connection leak gate | passed | full suite with `ResourceWarning` error |
| 20,000-row bounded-memory CSV run | passed | performance JSON/report |
| Python 3.12 lane | passed in CI | complete quality/package matrix |
| Python 3.13 lane | passed in CI | complete quality/package matrix |
| Ruff lint and format | passed in CI | no exclusions or generated baseline |
| strict MyPy | passed in CI | strict package check |
| Build, Twine and clean wheel install | passed in CI | Python 3.12/3.13 matrix |
| Real Binance small-range acceptance | passed | public unauthenticated download and deep integrity artifact |
| Multi-year real Parquet bounded-memory pipeline | passed | 721,597 BTCUSDT 5m rows, 83 partitions, 297 MiB peak RSS |
| Direct-vs-derived timeframe cross-check on official data | passed | 240,532 15m and 60,133 1h rows; exact OHLC/time/trades and <3e-16 relative sum error |
| Full BTCUSDT earliest-history adapter download | passed | 724,049 klines from exact first candle; 98 raw artifacts and deep integrity `ok` |
| Multi-year real update/resume/repair acceptance | passed | append-only v2, repaired v3, restart/resume and deep integrity evidence |
| Canonical repository integration | in progress | Draft PR and permanent CI are green; review/merge not authorized |

## Release conclusion

The static, Python 3.12/3.13, Hypothesis, Parquet, packaging, public Binance,
multi-year bounded-memory, official-data direct-vs-derived, complete earliest-history and
live update/resume/repair gates have passed. The component remains in a Draft PR because
human review and integration are not authorized. No merge, package publication, deployment
or live trading is implied by these acceptance results.
