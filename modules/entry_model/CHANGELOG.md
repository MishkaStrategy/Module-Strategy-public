# Entry Model Changelog

## 0.2.0 — local release candidate

### Added

- strict canonical setup schema 1 parsing;
- schema-2 EntryPlan and rejection contracts;
- deterministic full-semantic plan and rejection identities;
- source-feature fingerprint conflict detection;
- order-independent setup revision conflict handling;
- configuration fingerprint and state schema 2;
- stream identity, bar cursor and signature persistence;
- strict atomic checkpoint restore;
- bounded setup/source caches and structured counters;
- real Core registry, dependency planner, engine and checkpoint tests;
- property, adversarial, replay, load, benchmark and clean-wheel evidence;
- Codex handoff and standalone integration contract.

### Fixed

- plan-ID collisions across execution-relevant values;
- revision behavior that could depend on input ordering;
- anonymous malformed rejection collisions;
- subsecond stale-age truncation;
- publication/setup causality gaps;
- mutable nested metadata leakage;
- partial restore mutation and silent oversized-state trimming;
- bool acceptance in integer configuration fields;
- incomplete legacy plan diagnostics.

### Compatibility

- Core 0.4.x / Module API 1.0 public contracts;
- input setup schema 1;
- output plan/rejection schema 2;
- legacy plan schema 1 remains readable;
- state schema 1 is not silently migrated.

## 0.1.0

Initial independent setup-to-entry-plan implementation.
