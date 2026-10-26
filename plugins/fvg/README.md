# Modular Strategy FVG v0.3.0

Независимый аналитический producer-модуль Fair Value Gap для **Modular Strategy Core v0.4.x / Module API v1**.

Модуль не открывает сделки и не содержит логику конкретной торговой стратегии. Он обрабатывает только причинно доступные свечи, обнаруживает подтверждённые трёхсвечные FVG, поддерживает предварительную pre-FVG оценку, публикует детерминированный lifecycle зон и хранит ограниченное checkpoint-состояние.

Статус: **remote release validation in progress**. Merge, tag, release и публикация пакета этим документом не разрешаются.

## Подтверждённая геометрия

Для закрытых свечей `A, B, C` одного symbol/timeframe:

- bullish FVG: `C.low > A.high`, зона `[A.high, C.low]`;
- bearish FVG: `C.high < A.low`, зона `[C.high, A.low]`.

Равенство границ не считается gap. Подтверждённая зона доступна только после `C.close_time`; свеча `C` не может инвалидировать созданную ею зону.

## Публичный контракт

Import path:

```python
from modular_strategy_fvg import FvgModule
```

Entry point:

```text
modular_strategy.modules:fvg
```

Публикуемые features:

- `fvg.active_zone` — immutable активная зона; `feature_id == zone_id`;
- `fvg.zone_state` — детерминированный snapshot lifecycle зоны;
- `fvg.zone_created` — событие создания зоны;
- `fvg.zone_invalidated` — событие терминальной инвалидации;
- `fvg.evaluation` — результат подтверждённой evaluation pipeline, если публикация включена;
- `fvg.pre_candidate` — сохраняемый предварительный кандидат;
- `fvg.pre_status` — результат предварительной оценки или разрешения кандидата.

Feature schema остаётся `1`. Версия package/module — `0.3.0`; state schema — `4`.

## Базовая конфигурация

```yaml
modules:
  - class: modular_strategy_fvg:FvgModule
    enabled: true
    parameters:
      timeframes: [15m, 4h]
      direction_filter: both
      min_gap_absolute: "0"
      min_gap_fraction: "0"
      invalidation_level: full_fill
      invalidation_basis: wick
      require_consecutive_bars: true
      order_violation_policy: raise
      publish_lifecycle_events: true
      publish_evaluation_events: false
      pre_fvg_enabled: false
      max_zone_age_bars: 0
      max_active_zones_per_timeframe: 4096
      max_state_bytes: 8388608
```

Дополнительные параметры 0.3.0:

- gap filters: `gap_size_mode`, `min_gap_size`, `max_gap_size`, `tick_size`, `atr_period`, `average_range_period`;
- price filters: mode, bounds, anchor, fractional bands, match policy и target;
- volume filters: mode, lookback, ratio bounds, missing-data policy и target;
- pre-FVG: enable flag и evaluation offset;
- lifecycle: maximum zone age;
- `source_name` и typed `timeframe_overrides`.

Неизвестные параметры, строковые псевдо-boolean, scalar вместо массива `timeframes`, NaN/Infinity, некорректные enum, несовместимые filter-настройки и выход за bounded ranges отклоняются fail-closed.

## Confirmed evaluation и pre-FVG

Подтверждённая evaluation pipeline вычисляет расширенные метрики геометрии и импульса, применяет настроенные gap/price/volume filters и возвращает один из статусов:

- accepted;
- rejected;
- unavailable with current data.

Pre-FVG работает только на причинно доступном незавершённом интервале целевого timeframe. Кандидат хранит canonical identity, expected close time, metrics и filter traces. После появления подтверждающей свечи кандидат детерминированно разрешается как confirmed, filtered, disappeared, unavailable или missing confirmation bar.

## Lifecycle и invalidation

Lifecycle отслеживает:

- detected/active/touched/partially filled состояния;
- contact count, first/last touch;
- fill percentage и maximum penetration;
- age in bars;
- terminal invalidated, fully filled, expired и archived состояния.

`invalidation_level`:

- `first_touch` — ближняя граница;
- `midpoint` — середина зоны;
- `full_fill` — дальняя граница.

`invalidation_basis`:

- `wick` — high/low закрытой свечи;
- `close` — close закрытой свечи.

Безопасный baseline: `full_fill + wick`.

## Causality, adapters, gaps и ordering

- будущие или незакрытые свечи отклоняются;
- все внешние candle-like timestamps нормализуются строгим UTC boundary и naive datetime не принимаются;
- exact duplicate идемпотентен;
- conflicting duplicate с тем же close time и изменённым OHLCV вызывает `FvgDataConflictError`;
- изменение ранее обработанной исторической свечи вызывает canonical data conflict;
- временная регрессия вызывает `FvgDataOrderError` либо reset по явно выбранной политике;
- `data_gap=True` очищает соответствующие зоны/pre-candidates и создаёт causal barrier;
- после gap или legacy migration старые свечи не переиспользуются для нового трёхсвечного окна;
- один instance жёстко привязывается к одному symbol;
- working state применяется только после полной валидации и проверки размера checkpoint;
- output, IDs и checkpoint детерминированы при одинаковых входах.

## State schema v4 и restart

State schema v4 содержит:

- module version и config fingerprint;
- bound symbol;
- timeframe cursors, current-bar fingerprint и bounded recent-history fingerprints;
- gap/order barriers;
- активные зоны;
- lifecycle row для каждой активной зоны;
- unresolved pre-FVG candidates;
- pre-FVG evaluation cursors.

Ограничения:

- `max_active_zones_per_timeframe`;
- `max_state_bytes`;
- bounded retained fingerprint history;
- atomic отказ до commit, если candidate state превышает лимит.

`restore_state()` проверяет JSON-совместимость, структуру, canonical strings/decimals/timestamps, версии, config fingerprint, zone/candidate IDs, causal cursor coverage, lifecycle consistency, capacity и single-symbol scope.

Поддержаны строгие миграции:

- schema 1 / module 0.1.0 → schema 4;
- schema 2 / module 0.2.0 → schema 4;
- schema 3 / module 0.2.1 → schema 4 только для конфигурации, совместимой с прежним feature set.

Legacy cursors получают fail-closed barrier. После restore активные зоны, lifecycle state и unresolved pre-candidates публикуются один раз для восстановления FeatureStore.

## Downstream usage

```python
zone = view.features.latest(
    "fvg.active_zone",
    symbol=view.current.symbol,
    timeframe="15m",
    source_module="fvg",
)
```

Dependency planner должен размещать `fvg` раньше downstream-модуля, объявляющего требуемые FVG features.

## Проверка

Основные локальные команды:

```bash
cd plugins/fvg
python -m compileall src tests benchmarks scripts
ruff format --check src tests benchmarks scripts
ruff check src tests benchmarks scripts
mypy --strict src/modular_strategy_fvg
pytest
coverage run --branch --source=modular_strategy_fvg -m pytest
coverage report --fail-under=95
python -O -m pytest
python -m build
```

Remote exact-head release gate выполняется на self-hosted Linux runner отдельно для Python 3.12 и 3.13. Для каждой версии он проверяет:

- compileall, Ruff и strict MyPy;
- runtime regressions, полный pytest, branch coverage и `python -O`;
- bounded benchmark, memory и checkpoint/restart digest;
- две независимые нормализованные wheel/sdist сборки и byte-for-byte reproducibility;
- clean wheel и clean sdist install;
- Bandit, detect-secrets и pip-audit;
- reproducible CycloneDX SBOM.

Python 3.12 exact-head code-quality/behavioral pipeline уже была выполнена; расширенная двухверсионная release matrix и её evidence остаются текущим release blocker.

## Ограничения

- это аналитический producer, а не Entry/Risk/Execution/Position Management;
- OHLC не раскрывает внутрисвечный путь;
- один instance обслуживает один symbol;
- pre-FVG является provisional-сигналом, а не подтверждённой зоной;
- Core Module API остаётся версионируемым integration contract;
- релизный статус не повышается до получения зелёного exact-head evidence для полного release gate.
