# GitHub-Resumption Hardening — 30 Rounds

Each round included a concrete code or test change, relevant unit/regression/integration execution, determinism/no-look-ahead/checkpoint review, performance consideration, backward-compatibility review and a logical local commit. The rounds began only after Error Discovery defects were fixed.

| Round | Goal and material result |
|---:|---|
| 1 | Revalidated Decimal formulas and made weighted-average precision operand-order independent. |
| 2 | Strengthened exact BUY/SELL projected-risk symmetry and boundary tests. |
| 3 | Proved zero-TTL feedback visibility is limited to the immediate next completed bar. |
| 4 | Proved gap and exit priority override fresh scale-in approval without look-ahead. |
| 5 | Added strict public parser for approved feedback tied to the embedded plan. |
| 6 | Replaced ad-hoc planner feedback parsing with typed approved/rejected contracts. |
| 7 | Enforced constructor/parser equivalence for output DTOs. |
| 8 | Proved deterministic terminal feature IDs and payloads. |
| 9 | Made market-regime configuration strict, unique, sorted and order-independent in the fingerprint. |
| 10 | Deep-froze fallback instrument rules so mutation cannot invalidate a cached fingerprint. |
| 11 | Enforced lifecycle/pending invariants and recovery on unapproved position changes. |
| 12 | Made STOP_SCALING terminal and restart-safe. |
| 13 | Added state schema 4 integrity fingerprint and tamper detection. |
| 14 | Added approval-state integrity fingerprint, atomic restore and legacy migration. |
| 15 | Proved duplicate feedback after restore is idempotent. |
| 16 | Proved conflicting feedback and out-of-order replay fail closed. |
| 17 | Audited imports and confirmed only public `modular_core` API usage. |
| 18 | Fixed tuple-like restore compatibility with actual Core checkpoint helpers. |
| 19 | Fixed derivative notional by applying `contract_size`. |
| 20 | Published complete projected-position geometry in RiskRequest metadata and documented PS-CCR-001. |
| 21 | Profiled full plan/RiskRequest hot paths and added deterministic benchmark runner. |
| 22 | Reused the adjustment digest for the namespaced RiskRequest ID, improving 50k full-plan throughput by about 19%. |
| 23 | Verified bounded state under standard and extended load. |
| 24 | Verified deterministic restart load, equal final digest and bounded memory. |
| 25 | Added persistent trading counters while keeping restore count runtime-only. |
| 26 | Added bounded structured diagnostics and safe reason-code evidence. |
| 27 | Aligned package, manifests, VERSION, wheel/sdist and PEP 561 evidence at 0.2.2. |
| 28 | Added self-hosted macOS ARM64 workflow, security tools, SBOM and monorepo-path regression. |
| 29 | Exercised complete partial approval/checkpoint/restart/reconciliation/next-stage lifecycle. |
| 30 | Proved actual dependency order planner → Risk → approval and prepared Draft PR evidence. |

## Invariants preserved across all rounds

- completed-bar causal processing only;
- no direct exchange order placement or portfolio mutation;
- stop weakening forbidden;
- exact Decimal financial geometry;
- deterministic IDs, replay and checkpoints;
- bounded event/history caches;
- backward compatibility with Core 0.4.x / Module API 1.0;
- no Core strategy logic or source changes.
