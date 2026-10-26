# Архитектурная спецификация Market Regime v1.0.0

## Ответственность

Market Regime преобразует последовательность закрытых OHLCV-свечей одного инструмента в point-in-time features режима и волатильности.

В ответственность входят:

- вычисление EMA, Wilder ATR, ATR%-baseline и Efficiency Ratio;
- классификация trend/range/transition и low/normal/high volatility;
- hysteresis подтверждения;
- warm-up/readiness;
- обнаружение duplicate/out-of-order/gap;
- immutable feature contract;
- versioned checkpoint state;
- диагностика и причины skip.

Не входят:

- поиск точки входа;
- стоп, цель, риск и sizing;
- создание/отмена заявок;
- портфельная аллокация;
- агрегация таймфреймов;
- загрузка market data;
- оптимизация параметров;
- exchange/live connectivity.

## Компоненты

```text
MarketRegimeConfig
    ↓ validation + canonical parameters + signature
MarketRegimeModule (StrategyModule)
    ├─ event identity / symbol binding / gap policy
    ├─ update_indicators()
    ├─ regime and volatility hysteresis
    ├─ Feature DTO construction
    └─ RuntimeState snapshot/restore
          ↓
ModuleDecision(features, skips, diagnostics)
```

### `config.py`

Строгая конфигурация, диапазоны, timeframe parser, вычисление warm-up и SHA-256 подписи resolved parameters.

### `indicators.py`

Чисто алгоритмический слой. Rolling windows ограничены и обновляются O(1); baseline использует только предыдущие ATR%-наблюдения.

### `state.py`

Mutable runtime state принадлежит модулю. Сериализация наружу — JSON-compatible mapping. Восстановление строит новый объект, полностью валидирует его и только затем заменяет текущий state.

### `contracts.py`

Стабильные feature keys и `TypedDict` payload-схемы. Downstream-модули не должны импортировать внутренние индикаторные классы.

### `manifest.py`

Декларативный разъём Module API: version/API, required timeframe, produced features, warm-up, schema параметров и state schema.

## Входные данные

`MarketView`:

- `base_bars[-1]` — завершённое base-событие;
- `current_for(config.timeframe)` — последняя доступная закрытая целевая свеча;
- `data_gap` — сигнал ядра о разрыве base data.

Модуль не получает mutable store, broker или execution adapter.

## Выходные данные

`ModuleDecision` содержит только:

- 0 или 3 Feature;
- structured `skips`;
- diagnostics.

Поля `orders`, `signals`, `setups`, `cancellations`, `position_updates` и `closes` всегда пусты.

## Состояния

Логические фазы:

```text
EMPTY/WARMING_UP
    → RAW_CLASSIFICATION_AVAILABLE
    → CONFIRMING
    → STABLE_CLASSIFICATION
    ↘ GAP_RESET → WARMING_UP
```

Отдельно хранятся stable/candidate regime и stable/candidate volatility. Это две независимые state machines.

## Временные инварианты

1. `Candle.timestamp` трактуется как open time.
2. Feature `created_at == target_bar.close_time`.
3. Target bar не используется, если `target_bar.close_time > base_bar.close_time`.
4. Дубликат определяется точной identity open time + symbol + timeframe + `float.hex` OHLCV.
5. Target timestamps обязаны идти ровно через один configured timeframe; иначе indicator state сбрасывается.
6. Rolling volatility baseline вычисляется до добавления текущего ATR%, исключая self-inclusion и look-ahead.

## Ошибки и безопасный отказ

| Ситуация | Поведение |
|---|---|
| Empty view | skip `market_regime.empty_view` |
| Нет target bar | skip `market_regime.no_target_bar` |
| Target ещё не закрыт | skip `market_regime.incomplete_bar` |
| Недостаточно истории | skip `market_regime.not_ready` |
| Base gap | reset + skip `market_regime.data_gap_reset` |
| Target gap | reset + skip `market_regime.timeframe_gap_reset` |
| Exact duplicate event | idempotent skip + health counter |
| Conflicting duplicate | `MarketRegimeDataError` |
| Out-of-order | `MarketRegimeDataError` |
| Symbol change | `MarketRegimeDataError` |
| Повреждённый checkpoint | `MarketRegimeStateError`, старое состояние сохранено |

## Зависимости и циклы

Модуль зависит только от публичного `modular_core` API. Он не требует features других модулей и поэтому не может образовать входящий dependency cycle. Consumers зависят от его namespaced features, а не от внутренней реализации.

## Критерии приёмки

- подключается без изменения core;
- проходит ModuleRegistry validation;
- одинаковый input/config даёт одинаковые features/state;
- будущие/неполные свечи не влияют на результат;
- gap требует нового warm-up;
- state round-trip сохраняет следующее решение;
- повторное событие не создаёт duplicate feature;
- несколько symbol-инстанций изолированы;
- все критические правила имеют отдельные тесты;
- coverage не ниже 90%;
- полный набор core + module CI проходит.
