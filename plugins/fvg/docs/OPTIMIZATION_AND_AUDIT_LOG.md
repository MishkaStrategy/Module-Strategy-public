# Post-Error Optimization and Audit Log — FVG 0.2.1

These rounds began only after `ERROR_DISCOVERY_AUDIT.md`, regression fixes and the clean post-error baseline. Every round added a distinct executable proof. A full component suite was run after every second round and preserved as audit evidence.

Reference performance checkpoint unless a round states otherwise: 5,207.54 calls/s on the five-run 5k/15k hot-path comparison, 846,322 B comparison-harness median peak, 7,737 B bounded rolling peak, and identical restart digest.

## Thirty substantive rounds

| # | Goal | Executable strengthening | N/D/R and compatibility | Result |
|---:|---|---|---|---|
| 1 | strict bullish mathematics | Added strict equality exclusion and exact Decimal geometry proof. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 2 | bearish symmetry | Added mirrored bearish geometry and equality exclusion proof. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 1 — after rounds 1–2

- Full suite: **209 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 3 | future-data atomicity | Added future/incomplete rejection with unchanged state assertion. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 4 | timezone identity | Added non-UTC offset replay with byte-identical feature IDs. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 2 — after rounds 3–4

- Full suite: **211 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 5 | manifest input contract | Cross-checked resolved config, warm-up and declared timeframe inputs. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 6 | missing/stale input contract | Proved deterministic missing-timeframe skips and duplicate no-op state. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 3 — after rounds 5–6

- Full suite: **213 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 7 | immutable outputs | Proved feature value and metadata mutation attempts fail. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 8 | deterministic output lifecycle | Proved stable output order and IDs across independent runs. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 4 — after rounds 7–8

- Full suite: **215 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 9 | canonical configuration | Proved semantically equivalent Decimal parameters share state fingerprint. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 10 | unknown parameter rejection | Proved constructor fails closed for undeclared configuration. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 5 — after rounds 9–10

- Full suite: **217 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 11 | bounded lifecycle state | Proved capacity eviction and active-zone bound. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 12 | same-bar lifecycle ambiguity | Proved source candle C cannot invalidate its own new zone. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 6 — after rounds 11–12

- Full suite: **219 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 13 | schema-1 migration | Proved fail-closed migration to schema 3 with a causal barrier. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 14 | atomic corrupted restore | Proved partial history proof is rejected without state mutation. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 7 — after rounds 13–14

- Full suite: **221 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 15 | restart exact duplicate | Proved one-time republish then idempotent duplicate handling. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 16 | restart conflicting duplicate | Proved post-restore OHLCV conflict is rejected atomically. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 8 — after rounds 15–16

- Full suite: **223 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; no hot-path source change in this pair unless explicitly stated.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 17 | real Core registry | Loaded FVG through real ModuleRegistry and verified manifest versions. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 18 | entry-point contract | Resolved installed entry point to the public FvgModule class. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 9 — after rounds 17–18

- Full suite: **225 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 19 | Feature Store ownership | Published and exactly deleted an owned active-zone feature. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 20 | multi-instance isolation | Proved separate symbols produce isolated bindings and state digests. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 10 — after rounds 19–20

- Full suite: **227 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 21 | tail-cursor complexity | Proved cursor selection returns only the unseen suffix. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 22 | bounded canonical checkpoint | Proved empty state stays under configured bound and round-trips JSON. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 11 — after rounds 21–22

- Full suite: **229 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 23 | standard rolling workload | Proved retained cursor/history collections remain bounded over 100 events. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 24 | midpoint restart workload | Proved uninterrupted and restarted state digests match. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 12 — after rounds 23–24

- Full suite: **231 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 25 | structured diagnostics | Proved diagnostic field presence and stable counter types. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 26 | typed reason codes | Cross-checked emitted delete reason against FvgReasonCode. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 13 — after rounds 25–26

- Full suite: **233 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 27 | packaging quality contract | Proved PEP 561 marker, strict mypy and 95% coverage metadata. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 28 | self-hosted workflow contract | Parsed workflow and verified labels, timeout, concurrency, security and cleanup. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 14 — after rounds 27–28

- Full suite: **235 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.

| 29 | full lifecycle replay | Proved complete output/delete/state digest is deterministic across runs. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |
| 30 | release-document consistency | Cross-checked README, changelog and completeness evidence with runtime version. | No-look-ahead, deterministic replay, checkpoint/restore and Core 0.4 compatibility were unchanged or directly exercised; no public feature key/schema change. | PASS |

### Audit 15 — after rounds 29–30

- Full suite: **237 passed**.
- Public contracts: feature keys and feature schema 1 unchanged.
- Invariants: completed bars, no-look-ahead, determinism, bounded state and atomic restore remain green.
- Coverage reference: post-error branch-aware baseline 96%; final coverage is recomputed after round 30.
- Performance reference: 5,207.54 calls/s; hot-path comparison remains inside the 5% gate.
- Peak memory reference: 7,737 B bounded rolling allocation; checkpoint remains bounded.
- Backward compatibility: state schemas 1/2 remain readable; outputs remain schema 1.
- Core/neighbor compatibility: Core 0.4 API only; no hidden cross-module dependency.
- GitHub workflow: not yet pushed during this local audit.
- New defects: none. Decision: continue.
