# Breakout–Return module — hardened 1.0.0 contract

## Назначение

`BreakoutReturnModule` обнаруживает пробой причинно доступного диапазона и последующий контролируемый возврат внутрь frozen snapshot этого диапазона.

Направление результата контртрендовое:

- пробой верхней границы и допустимый возврат → `SELL`;
- пробой нижней границы и допустимый возврат → `BUY`.

Модуль является аналитическим producer-модулем. Он публикует setup, signal и immutable features, но не размещает заявки, не рассчитывает entry/stop/target/position size и не изменяет портфель.

## Границы ответственности

Модуль:

- работает с закрытым `MarketView.current`;
- использует только point-in-time features из Feature Store;
- хранит собственный bounded state;
- детерминированно обрабатывает duplicate/replay/restart;
- подключается через существующие `StrategyModule`, `ModuleManifest` и `ModuleDecision`;
- не читает файлы, интернет или системное время;
- не содержит биржевой, инструментальной или portfolio-логики.

Entry, Risk, Protection, Exit, Execution и accounting находятся за пределами модуля.

## Временная семантика и no-look-ahead

`Candle.timestamp` — время открытия свечи. Решение становится доступно после `Candle.close_time`.

В rolling-режиме reference range строится только из предыдущих `lookback_bars`. Текущая свеча добавляется в rolling state после проверки пробоя. Текущие и будущие экстремумы не могут изменить уже принятое решение.

Свеча пробоя не может одновременно подтвердить возврат. Возврат разрешён только на последующих закрытых барах `1…max_return_bars`.

Проверки включают:

- truncated-stream invariance;
- future-suffix invariance;
- BUY/SELL symmetry;
- запрет same-bar confirmation;
- point-in-time feature availability.

## Reference modes

### `rolling`

Диапазон строится из high/low предыдущих `lookback_bars`.

Монотонные очереди обеспечивают:

- амортизированное O(1) обновление экстремумов;
- память, ограниченную `lookback_bars`;
- отсутствие полного O(history) пересчёта на событие.

### `feature`

Модуль читает `reference_feature_key`. Значение должно быть mapping:

```python
{
    "high": 100.0,
    "low": 90.0,
    "range_id": "ny-range-2026-07-20",  # optional, рекомендуется
}
```

Опциональные усиления input contract:

- `reference_feature_source_module` — принимать feature только указанного producer;
- `reference_feature_timeframe` — принимать feature только указанного timeframe;
- `reference_feature_schema_version` — сравнивать с `feature.metadata["schema_version"]`;
- `max_reference_age_seconds` — отклонять слишком старый feature.

Все усиления отключены по умолчанию. Поэтому существующая producer-agnostic конфигурация сохраняет поведение и исходный default fingerprint.

При заданном `reference_feature_source_module` одинаковый key от другого producer не подменяет intended range. Foreign-only значение трактуется как `breakout_return.reference_missing`.

Producer binding:

- не импортирует конкретный upstream-модуль;
- участвует в configuration fingerprint;
- предотвращает restore checkpoint под другим producer contract;
- сохраняет replaceability и Module API boundary.

После arm ownership остаётся частью reference contract:

- `range_change_policy="invalidate"` инвалидирует active setup при исчезновении intended producer, даже если foreign producer публикует feature с тем же key;
- `range_change_policy="keep_snapshot"` сохраняет frozen intended range и игнорирует foreign replacement.

Подробный контракт: `docs/modules/breakout_return_feature_source_contract.md`.

## Lifecycle

Внутренние состояния:

- `scanning` — ожидание пробоя;
- `waiting_return` — активный пробой и отслеживание return/extreme/TTL.

```text
ARMED -> CONFIRMED
      -> REJECTED     return depth > max_return_depth
      -> EXPIRED      bars_since_breakout > max_return_bars
      -> INVALIDATED  gap, missing/changed/stale/incompatible reference
```

После терминального события active state очищается, и модуль возвращается в `scanning`.

## Пробой

`breakout_confirmation`:

- `body` — open и close находятся за buffered boundary;
- `close` — достаточно close за boundary.

```text
upper = range_high × (1 + breakout_buffer_bps / 10_000)
lower = range_low  × (1 - breakout_buffer_bps / 10_000)
```

Сравнения строгие. Равенство boundary пробоем не считается.

## Возврат

```text
above breakout: (range_high - return_close) / range_width
below breakout: (return_close - range_low) / range_width
```

Допустимый интервал включительный:

```text
min_return_depth <= return_depth <= max_return_depth
```

`return_confirmation`:

- `body` — open и close return-свечи находятся внутри диапазона;
- `close` — достаточно close внутри диапазона.

Финансово значимые сравнения выполняются через `Decimal`.

## Публичные outputs

### `breakout_return.active_setup`

Immutable feature активного пробоя. При изменении breakout extreme публикуется новая deterministic revision, а предыдущая удаляется точно по `feature_id`.

### `breakout_return.confirmed`

Однобарный confirmation feature, доступный downstream-модулям на том же закрытом баре.

Также публикуются:

- `SetupEvent(CONFIRMED)`;
- `Signal`;
- typed diagnostics и reason codes.

Поля `orders`, `cancellations`, `closes` и `position_updates` остаются пустыми.

## Reason codes

- `breakout_return.insufficient_reference`;
- `breakout_return.duplicate_event_ignored`;
- `breakout_return.data_gap_reset`;
- `breakout_return.breakout_above`;
- `breakout_return.breakout_below`;
- `breakout_return.return_confirmed`;
- `breakout_return.return_too_deep`;
- `breakout_return.return_expired`;
- `breakout_return.reference_changed`;
- `breakout_return.reference_missing`;
- `breakout_return.reference_stale`;
- `breakout_return.reference_schema_mismatch`.

## Конфигурация

| Параметр | Default | Ограничение |
|---|---:|---|
| `reference_mode` | `rolling` | `rolling`, `feature` |
| `reference_feature_key` | `range.current` | trimmed namespaced string |
| `reference_feature_source_module` | `null` | trimmed nonblank string или null; только feature mode |
| `reference_feature_timeframe` | `null` | nonblank string или null; только feature mode |
| `reference_feature_schema_version` | `null` | nonblank string или null; только feature mode |
| `max_reference_age_seconds` | `null` | integer `1…10_000_000_000` или null; только feature mode |
| `lookback_bars` | `20` | integer `2…100_000` |
| `direction` | `both` | `both`, `above`, `below` |
| `breakout_confirmation` | `body` | `body`, `close` |
| `return_confirmation` | `body` | `body`, `close` |
| `breakout_buffer_bps` | `0` | finite int/float/Decimal `0…5000` |
| `min_return_depth` | `0.02` | finite int/float/Decimal `0…1` |
| `max_return_depth` | `0.20` | finite int/float/Decimal `min…1` |
| `max_return_bars` | `36` | integer `1…100_000` |
| `range_change_policy` | `invalidate` | `invalidate`, `keep_snapshot` |

`bool`, NaN, infinity, float вместо integer field, whitespace-only strings и invalid parameter dependencies отклоняются до запуска.

## Duplicate, ordering и gap

- exact duplicate возвращает `duplicate_event_ignored` без мутации causal state;
- numeric-equivalent OHLCV имеет одну canonical signature;
- conflicting duplicate вызывает `BreakoutReturnDataError`;
- out-of-order bar вызывает `BreakoutReturnDataError`;
- symbol/timeframe switch внутри экземпляра отклоняется;
- `data_gap` инвалидирует active setup и сбрасывает warm-up;
- validation failure атомарен и не меняет checkpoint.

## Checkpoint и restore

Публичный manifest остаётся `version="1.0.0"`, `state_schema_version="1"` для совместимости с Core 0.4.0.

Внутренний payload version — `2`. Он содержит:

- configuration fingerprint;
- sequence/history count;
- monotonic extrema;
- active setup;
- last bar time/canonical signature;
- bound symbol/timeframe;
- bounded causal counters.

Restore:

- принимает legacy payload v1;
- поддерживает двухэтапную цепочку `v1 active -> v2 -> second restart`;
- проверяет exact keys, fingerprint, geometry, extrema horizon и stream binding;
- отклоняет cross-producer state при настроенном producer binding;
- валидирует temporary state до замены live state;
- отклоняет partial/oversized/corrupted/incompatible state без частичной мутации.

## Подключение

```yaml
modules:
  - class: strategy_modules.breakout_return:BreakoutReturnModule
    parameters:
      reference_mode: feature
      reference_feature_key: session_range.completed
      reference_feature_source_module: session_range
      reference_feature_timeframe: 5m
      reference_feature_schema_version: "1"
      max_reference_age_seconds: 86400
      min_return_depth: 0.02
      max_return_depth: 0.20
      max_return_bars: 36
```

Entry point:

```text
modular_strategy.modules / breakout_return
```

Пакет содержит PEP 561 marker `py.typed`.

## Проверки

Semantic head: `0d81bd6367181d323b79874badb4cb91d2a5b5c3`.
Exact evidence snapshot: self-hosted run `29838403367` at parent head `cc211b4801650f70937272c26a681e2d75b1e07e`.

- Python 3.12: 131 component tests passed;
- Python 3.13: 131 component tests passed;
- full repository: 312 tests passed;
- failures/errors/skips: 0;
- statement coverage: 90.856031%, displayed as 91%, gate 90%;
- Ruff, strict mypy, compileall: passed;
- wheel, sdist, clean install, `pip check`, entry point, `py.typed`: passed;
- Bandit: passed;
- detect-secrets: zero findings;
- pip-audit: 70 dependencies, zero known vulnerabilities;
- CycloneDX SBOM: 74 components;
- exact artifact digests: `sha256:add18e5a7679756f3c7c59b66fa8b8ee5cd2dce75f3eb2b4c89e33beca08d7c5` and `sha256:e4850ed74150a8186eb4457d54ba53edf5d523cfaf9b651990013fc755efe5d8`.

## Benchmark

Python 3.13.14, 1,000 events, five runs:

- median: `0.058070167 s`;
- minimum: `0.057790542 s`;
- maximum: `0.061532916 s`;
- throughput: `17,220.55 events/s`;
- median peak memory: `6,743 B`;
- checkpoint: `920 B`;
- restore median: `27.833 µs`;
- deterministic final and semantic state digests across all runs.

Extended restart evidence remains bounded through 1,000,000 events.

## Известные ограничения

1. Completed-bar authenticity обеспечивает Core/data-feed contract.
2. Один экземпляр обслуживает один symbol/timeframe.
3. OHLC не раскрывает точный intrabar path; same-bar return запрещён.
4. Публичный manifest version остаётся 1.0.0 до согласованного Core migration hook.
5. Полный production verdict зависит от Entry/Risk/Protection/Exit/Execution и paper/live цепочки.
