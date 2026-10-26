# Changelog

## 1.0.0 — 2026-07-20

### Added

- независимый `MarketRegimeModule` для Module API v1;
- trend/range/transition classification;
- relative ATR volatility classification;
- independent regime/volatility hysteresis;
- immutable typed feature contracts;
- strict configuration and exact Decimal parameter identity;
- duplicate/out-of-order/gap protection;
- versioned transactional checkpoint state;
- structured skips, diagnostics and health counters;
- 80 unit/integration/regression tests;
- benchmark, architecture, completeness, optimization and audit documentation;
- отдельный CI на Python 3.12/3.13 с coverage, strict mypy, compileall и wheel build;
- автоматическая выгрузка pytest-лога при падении CI.

### Fixed during development

- flat-market zero ATR readiness;
- delayed gap reset without target bar;
- base event versus repeated HTF event ambiguity;
- integer OHLC fingerprint regression;
- partially saved event identity;
- causal timestamp inconsistency in restored state;
- semantically equal Decimal values producing different signatures;
- test fixture приведена к фактическим четырём полям `ReadinessView` Core v0.4;
- test fixture теперь явно передаёт обязательные `position` и `pending_orders` в `MarketView`.

### Verification

- Market Regime test matrix: Python 3.12 — passed; Python 3.13 — passed;
- Core regression matrix: Python 3.12 — passed; Python 3.13 — passed;
- 80 tests, 93% statement coverage, strict mypy, compileall и wheel — passed.
