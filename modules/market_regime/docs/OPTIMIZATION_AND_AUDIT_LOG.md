# Market Regime 1.2.0 — Resumption Hardening Log

Date: 2026-07-21

Baseline: post-error-fix Market Regime 1.2.0. The zero-stage Error Discovery Audit is documented separately and is not counted among the rounds below.

## Thirty substantive rounds

| Round | Area | Executable result | Verification |
|---:|---|---|---|
| 1 | Decimal identity | Equivalent Decimal representations share one semantic signature. | Parameterized regression grid. |
| 2 | Mathematical symmetry | Bullish/bearish classifiers remain symmetric at boundary values. | Exact threshold and sign-grid tests. |
| 3 | UTC invariance | Equivalent UTC instants produce identical feature traces. | Cross-offset replay test. |
| 4 | Completed-bar timing | Target becomes available exactly at completed close. | No-look-ahead boundary regression. |
| 5 | Timeframe aliases | Canonical aliases resolve to one target stream. | Alias-history regression. |
| 6 | Unrelated histories | Invalid unrelated histories cannot shadow the configured target. | Input-selection regression. |
| 7 | Output payloads | Feature payloads remain JSON-safe and finite. | Recursive payload validation. |
| 8 | Restart identity | Feature identity survives restart and diagnostics toggle. | Continuous versus restored trace equality. |
| 9 | Strict constructor | Unknown configuration fields fail closed. | Typed constructor rejection. |
| 10 | Config fingerprint | All Decimal fields use canonical semantic identity. | Signature and parameter equality tests. |
| 11 | Hysteresis | Candidate progression and confirmation are explicit. | State-count and first-publication checks. |
| 12 | Gap lifecycle | Gap reset clears bounded runtime and preserves cumulative health. | Gap-state regression. |
| 13 | Partial checkpoint | Partial cursor identity is rejected atomically. | Parameterized state corruption tests. |
| 14 | Restore ownership | Restored state detaches from caller-owned containers. | Post-restore mutation test. |
| 15 | Conflicting duplicate | Conflicting target rejection no longer advances the base cursor. | Failing regression first, corrected retry succeeds. |
| 16 | Out-of-order target | Target ordering rejection is transactional. | Checkpoint before/after equality and corrected retry. |
| 17 | Core registry | Real Core registry loads the public import path. | Core 0.4.0 registry test. |
| 18 | Dependency planner | Mapping config and planner resolve exact timeframe and feature ownership. | Core planner contract test. |
| 19 | Feature Store | All three owned feature keys are same-bar consistent downstream. | Real Feature Store test. |
| 20 | Instance isolation | Interleaved instruments remain isolated when one instance restarts. | Independent baseline trace comparison. |
| 21 | Bounded state | Checkpoint size plateaus at 1k, 5k and 10k events. | Less than 256-byte spread and 16 KiB ceiling. |
| 22 | Evidence robustness | Benchmark verification no longer depends on optimized-away assertions. | AST regression and `python -O` safe invariants. |
| 23 | Standard load | Repeated 50,000-event benchmark checked in. | Deterministic output/state digests. |
| 24 | Extended load | 250,000-event continuous and midpoint-restart runs completed. | Equal digests, publications, features and bounded checkpoint. |
| 25 | Typed observability | Every public skip path exposes the matching `SkipReason`. | Complete skip-reason matrix. |
| 26 | Health metrics | Health counters remain monotonic through reset and restart. | Pairwise monotonicity regression. |
| 27 | Security evidence | Reproducible CycloneDX SBOM and reviewed secret-scan evidence added. | SBOM and detect-secrets regressions. |
| 28 | Self-hosted CI | Bounded self-hosted macOS/ARM64 workflow added. | Workflow contract test, concurrency and timeouts. |
| 29 | Installed lifecycle | Fresh wheel-only lifecycle and midpoint restart proved. | Public import, entry point, registry, causality and 5,000-event trace. |
| 30 | Release evidence | Hash-verified evidence inventory and final quality gates assembled. | 151 source tests, 93% coverage and clean package checks. |

## Fifteen intermediate audits

### Audit 1 — rounds 1–2

- Decimal canonicalization and directional symmetry passed.
- Public contracts unchanged.
- Unit, regression and determinism checks passed.
- Decision: continue.

### Audit 2 — rounds 3–4

- UTC, completed-bar semantics and no-look-ahead passed.
- Exact target-close availability verified.
- Decision: continue.

### Audit 3 — rounds 5–6

- Alias resolution and target-history selection passed.
- No namespace or producer ambiguity found.
- Decision: continue.

### Audit 4 — rounds 7–8

- Immutable JSON-safe outputs and deterministic IDs passed.
- Restart and diagnostics-toggle traces are identical.
- Decision: continue.

### Audit 5 — rounds 9–10

- Strict configuration and canonical fingerprint passed.
- Unknown fields and unsafe coercions fail closed.
- Decision: continue.

### Audit 6 — rounds 11–12

- Hysteresis lifecycle and gap reset passed.
- State remains bounded and cumulative counters are preserved.
- Decision: continue.

### Audit 7 — rounds 13–14

- Corrupted/partial checkpoint rejection is atomic.
- Restored state owns its data and supports schema-1 migration.
- Decision: continue.

### Audit 8 — rounds 15–16

- MR-ERR-006 fixed: conflicting and out-of-order target rejections are transactional.
- Corrected retry at the same base event succeeds.
- Full suite and restart benchmark passed.
- Decision: continue.

### Audit 9 — rounds 17–18

- Real Core registry, config loader and dependency planner contracts passed.
- No Core changes requested.
- Decision: continue.

### Audit 10 — rounds 19–20

- Feature Store consistency and multi-instance isolation passed.
- No cross-symbol state leakage found.
- Decision: continue.

### Audit 11 — rounds 21–22

- Checkpoint plateau and active benchmark invariants passed.
- Ruff, strict mypy, compileall and Bandit passed.
- Decision: continue.

### Audit 12 — rounds 23–24

- 50,000-event repeated run and 250,000-event restart equivalence passed.
- Continuous/restarted digests and counts match.
- The incomplete million-event invocation was not counted.
- Decision: continue.

### Audit 13 — rounds 25–26

- Every skip reason is typed and health counters are monotonic.
- Quiet/verbose semantic equivalence remains intact.
- Decision: continue.

### Audit 14 — rounds 27–28

- Source/sdist security evidence passed.
- Self-hosted-only workflow uses macOS/ARM64 preflight, sequential Python matrix, concurrency cancellation, timeouts, retention and cleanup.
- Actual custom runner labels remain externally unverified.
- Decision: continue.

### Audit 15 — rounds 29–30

- Source suite: 151 passed; statement coverage: 93% against a 90% gate.
- Extracted sdist: 150 passed and one expected repository-workflow skip.
- Ruff lint/format, strict mypy, compileall, Bandit, wheel, sdist, Twine and clean install passed.
- Final benchmark: 50,000 events × 3, median 10,235.64 events/s.
- Restart at 25,000 preserved exact output/state digests; restore about 0.371 ms.
- Peak traced allocation: 59,495 bytes; checkpoint: 3,511 bytes.
- Public feature schema 1, state schema 2 and Core 0.4 compatibility remain unchanged.
- External gates: Python 3.12 self-hosted CI, full remote repository suite and fresh Actions evidence.
- Decision: local hardening complete; keep PR Draft until external gates pass.

## Final local gate summary

| Gate | Result |
|---|---|
| Tests | 151 passed |
| Coverage | 93%, gate 90% |
| Ruff lint/format | PASS |
| strict mypy | PASS |
| compileall | PASS |
| Bandit | PASS |
| Wheel and sdist | PASS |
| Twine metadata | PASS |
| Clean install and `pip check` | PASS |
| Public import and entry point | PASS |
| Checkpoint/restore | PASS |
| Deterministic replay | PASS |
| No-look-ahead | PASS |
| Self-hosted GitHub CI | Pending remote execution |

No merge, stable tag, release or production-live claim is authorized by this document.
