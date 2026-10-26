# Архитектурная спецификация FVG Module v0.1.0

## Ответственность

FVG Module — независимый feature producer. Он преобразует закрытые OHLC-свечи в point-in-time факты о Fair Value Gap.

Входит:

- строгая трёхсвечная детекция bullish/bearish FVG;
- фильтрация направления и минимального размера;
- жизненный цикл активной зоны;
- конфигурируемая инвалидация;
- gap/order/idempotency protection;
- diagnostics и skip reasons;
- snapshot/restore;
- ModuleManifest и plugin entry point.

Не входит:

- подтверждение входа;
- выбор позиции, stop, target, quantity;
- режим рынка, sweep, breakout/return;
- агрегация HTF;
- exchange rounding;
- торговое исполнение.

## Компоненты

```text
FvgConfig
   ↓
detect_zone(A, B, C)
   ↓
FvgZone (Decimal state)
   ↓
FvgModule lifecycle
   ├─ fvg.active_zone
   ├─ fvg.zone_created
   └─ fvg.zone_invalidated / FeatureDelete
```

- `config.py`: enum и строгая конфигурация;
- `detector.py`: stateless-алгоритм;
- `models.py`: domain model и сериализация;
- `module.py`: Module API adapter, state machine, diagnostics;
- `__init__.py`: стабильный публичный экспорт.

## Вход

`MarketView`:

- только закрытый `current` base bar;
- `timeframe_bars` из gap-safe Core aggregator;
- `data_gap`;
- immutable market snapshot.

Модуль не получает broker, mutable stores или future candles.

## Выход

`ModuleDecision`:

- `features`;
- `feature_deletes`;
- `skips`;
- `diagnostics`.

Orders, signals, setups и position updates всегда пусты.

## Состояния зоны

```text
ABSENT
  ↓ three completed consecutive candles
ACTIVE
  ├─ remains active
  ├─ INVALIDATED by configured threshold
  └─ CLEARED by data gap / safe order reset
```

Lifecycle не использует скрытые переходы. `zone_id` — SHA-256 derived identity из symbol, timeframe, direction, C open time и Decimal-границ.

## Временные инварианты

1. Zone создаётся не раньше `C.close_time`.
2. Исходная C-свеча не может инвалидировать созданную ею зону.
3. Инвалидация проверяется только последующими закрытыми свечами.
4. Future/incomplete timeframe candle отклоняется.
5. Duplicate latest close не меняет state.
6. FVG не строится через gap при `require_consecutive_bars=True`.
7. После data gap retained history не пересканируется как новая.

## Интеграция

Manifest динамически отражает configured timeframes и warm-up `3` бара. `skip_until_ready=False`, чтобы каждый timeframe становился доступен независимо и gap reset был виден до общего warm-up всех timeframe.

Dependency planner ставит FVG раньше любого модуля, объявившего `required_features=("fvg.active_zone",)`. Downstream consumer получает новую зону на том же закрытом base bar.

Публичные типы Module API экспортируются Core с PEP 561 marker `src/modular_core/py.typed`. Это единственное изменение пакета Core, необходимое для строгой cross-package проверки; код и сигнатуры ядра не менялись. Локальный parallel stub не создаётся.

## Безопасный отказ

- malformed config → `ValueError` до запуска;
- temporal regression → `FvgDataOrderError` или `reset_and_skip`;
- malformed checkpoint → `FvgStateError` без частичного восстановления;
- data gap → state clear + exact deletes + cursor barrier;
- empty base history → structured skip;
- missing timeframe history → structured skip;
- incompatible state/config → restore rejected before commit.

## Критерии готовности

- no kernel logic changes;
- Core typing metadata published through PEP 561;
- strict formulas and equality boundaries tested;
- no-lookahead and no-cross-gap regressions tested;
- exact feature IDs/deletes tested;
- checkpoint roundtrip and restart republish tested;
- real ModuleRegistry/BacktestEngine integration tested;
- entry-point discovery tested;
- same-bar downstream feature visibility tested;
- Python 3.12/3.13 CI, Ruff, strict mypy, coverage ≥90%, benchmark and wheel build passed;
- full Core CI passed on Python 3.12/3.13.
