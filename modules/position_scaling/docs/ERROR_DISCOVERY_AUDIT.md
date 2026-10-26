# Error Discovery Audit — Position Scaling & Adjustment

## Scope

Mandatory zero-stage audit performed before the new 30 hardening rounds. The local release candidate 0.2.0 was compared with the incomplete remote 0.1.0 snapshot. The local candidate was preserved as source of truth. Core source was not modified.

## Baseline

- integration base: `integration/modular-strategy-v0.5.0` at `5e03b933207a99940244634f49a756d27888a5c3`;
- Core under local validation: 0.4.1;
- Module API: 1.0;
- local candidate before fixes: 0.2.0;
- remote early snapshot: commit `5c2cd71676691a5533f5c5da3d77620944e14ca8`, version 0.1.0;
- intended target: standalone package under `modules/position_scaling`.

## Confirmed defects

| ID | Severity | Symptom / root cause | Fix and regression evidence |
|---|---|---|---|
| PS-ERR-001 | High | Partial Risk approval was not fed back to the planner, so a correctly filled approved delta could remain `PARTIALLY_FILLED`. | Persist typed approved quantity and reconcile against it; restart/partial-approval tests added. |
| PS-ERR-002 | High | Position quantity above the approved target was accepted as completion. | Overfill now enters `RECOVERY_REQUIRED`; long/short and scale-out tests added. |
| PS-ERR-003 | High | Full scale-out could compute a zero residual and raise `ValueError`. | Fail closed with `invalid_quantity`; regression test added. |
| PS-ERR-004 | High | Rebalance reduction followed scale-in safety checks. | Safety path is selected from computed direction; max-scale-out regression added. |
| PS-ERR-005 | High | DTO values could disagree with embedded plan/risk evidence. | Strict typed constructors/parsers validate action, direction, quantity, identity, setup, symbol, price and decision schema. |
| PS-ERR-006 | High | `Decimal.normalize()` used the ambient precision and could silently truncate long financial values during serialization. | Context-independent lossless fixed-point canonicalization; >28-digit regression added. |
| PS-ERR-007 | High | Risk rejection did not reliably clear pending lifecycle. | Typed rejection feedback terminates and clears pending state with cooldown. |
| PS-ERR-008 | Medium | Exact duplicate approval feedback could be recorded repeatedly. | Feedback fingerprint makes replay idempotent. |
| PS-ERR-009 | High | A newer unrelated feedback feature could mask the matching pending adjustment. | All active feedback features are scanned and matched by adjustment identity. |
| PS-ERR-010 | High | Legacy state with previously truncated derived average price could fail after DTO hardening. | Schema 1–3 migration rebuilds derivable projected geometry and upgrades to schema 4. |
| PS-ERR-011 | High | Setup identity and RiskDecision schema were not strict enough. | Exact setup/symbol/request/schema validation in approval module. |
| PS-ERR-012 | High | A factual position change could reconcile without terminal approval feedback. | Any unapproved quantity/entry change enters recovery. |
| PS-ERR-013 | Medium | `STOP_SCALING` could leave an eternal pending plan. | It is now terminal, clears pending state and is restart-safe. |
| PS-ERR-014 | High | Core checkpoint helpers deep-freeze arrays to tuples, while restore accepted only lists. | Strict non-string `Sequence` restore support for planner and approval states. |
| PS-ERR-015 | High | Current notional in the adjustment RiskRequest ignored derivative `contract_size`. | Current/projected notional include contract size; regression tests added. |
| PS-ERR-016 | Medium | A persisted restore counter changed state merely by restoring it. | Restore count is runtime-only diagnostics and not part of trading checkpoint. |
| PS-ERR-017 | Medium | Regime configuration and fallback instrument rules were not fully canonical/immutable. | Strict normalized regimes and deep-frozen instrument mapping. |
| PS-ERR-018 | CI | Standalone workflow-path assertion failed after monorepo placement. | Test resolves both standalone and `../../.github` locations. |
| PS-ERR-019 | Evidence | Benchmark artifact reported stale version 0.2.1 after bump to 0.2.2. | Benchmark generator aligned to 0.2.2. |

## Revalidation after fixes

- 228 tests passed;
- branch-aware coverage 90%, gate 90%;
- Ruff and formatter passed;
- strict mypy passed for 9 source files;
- compileall passed;
- wheel and sdist built;
- clean-wheel installation with Core 0.4.1 passed;
- public imports, both entry points and checkpoint smoke passed;
- Bandit and detect-secrets passed locally;
- local `pip-audit` was not completed because the container could not resolve PyPI; it remains mandatory in self-hosted CI.

## Result

Critical/high defects found by this audit were fixed and covered before completeness and optimization rounds began. Remaining cross-component risk semantics are documented separately as PS-CCR-001.
