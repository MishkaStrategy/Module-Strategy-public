# Modular Strategy Fibonacci v0.4.0

Независимый аналитический plugin-модуль последовательного Fibonacci для **Modular Strategy Core v0.4.x**.

Модуль обрабатывает только закрытые свечи и ведёт long/short Fibonacci как поток состояний без знания будущих баров. Он публикует только features: заявок, сделок, отмен и изменений позиции не создаёт.

## Главное правило endpoint

- long: уровень `1` ставится в начало импульса на дне, `0` — в окончательный high;
- short: уровень `1` ставится в начало импульса на вершине, `0` — в окончательный low;
- первое касание `-0.18/-0.27` не замораживает endpoint;
- пока последующая коррекция не подтвердила завершение, новый favorable extreme переносит `0` дальше;
- неоднозначная OHLC-свеча не может одновременно обновить endpoint и подтвердить его.

Рабочие коррекции: `0.5`, `0.618`, `0.786`, `0.886`. Продолжения: `-0.18`, `-0.27`, `-0.5`, `-0.618`. Пробой уровня `1` отменяет волну.

## Produced features

- `fibonacci.active_projection` — текущая immutable-сетка и ревизия полного endpoint;
- `fibonacci.wave_completed` — точечный lifecycle-факт завершения волны;
- `fibonacci.short_continuation_setup` — аналитический 4h/1d short-факт, прошедший historical, recent-regime, current-level и linked-root gates.

Short setup v0.4.0 требует:

1. достаточной исторической выборки, success rate и непрерывного хвостового streak;
2. recent window не ниже порога и не в состоянии `weakening`;
3. отдельной достаточной статистики именно активного уровня `0.5` или `0.618`;
4. совпадения `sequence_root_id` активной волны с root непрерывного исторического хвоста.

`setup_id` — SHA-256 fingerprint symbol/timeframe, anchors, endpoint revision, correction, historical/recent evidence, окон и thresholds. Изменение факта или конфигурации создаёт новый feature ID.

Все цены и уровни передаются Decimal-строками. Features имеют детерминированные ID, gap invalidation и versioned schemas.

## Установка из монорепозитория

```bash
python -m pip install -e .
python -m pip install -e './plugins/fibonacci[dev]'
```

Entry point сохраняется в группе `modular_strategy.modules`:

```text
fibonacci = modular_strategy_fibonacci:FibonacciModule
```

## YAML

```yaml
modules:
  - plugin: fibonacci
    enabled: true
    parameters:
      timeframes: [15m, 1h, 4h, 1d]
      dataset_hash: core-stream-unhashed
      min_atr_multiple: "1.5"
      publish_short_setup: true
      short_reaction_lookback: 40
      short_reaction_minimum_samples: 8
      short_reaction_minimum_success_rate: "0.65"
      short_reaction_minimum_streak: 3
      short_regime_window_size: 12
      short_regime_minimum_window_samples: 8
      short_regime_minimum_success_rate: "0.65"
      short_regime_minimum_level_samples: 4
      short_regime_minimum_level_success_rate: "0.65"
      short_regime_change_threshold: "0.15"
```

## Core compatibility retained from Draft PR #243

- namespace `modular_strategy_fibonacci`;
- strict candle/Core boundary protocol;
- tail-only incremental processing;
- gap cursor barrier;
- tuple/list-compatible restore после Core `deep_freeze`;
- настоящие `ModuleRegistry` и `BacktestEngine` integration tests;
- Python 3.12/3.13 plugin CI with Ruff, strict MyPy, branch coverage and wheel build.

## Research helpers

`scan_bearish_reactions` возвращает versioned, JSON-ready evidence для BTC/ETH/SOL или других point-in-time snapshots. `history_only_walk_forward` группирует frozen cutoffs по symbol/timeframe и на каждом cutoff использует только историю, уже находящуюся в этом snapshot. Оба helper-а возвращают только аналитические факты; поля orders/trades в scanner payload всегда пусты.

Подробности: `docs/BEARISH_REACTION_REGIME.md`, `docs/CORE_INTEGRATION.md` и `reports/cycle4-regime-evidence.md`.

## Ограничения

- результатов PnL или доходности нет;
- history-only walk-forward sample локального candidate мал и подтверждает логику, а не прибыльность;
- на финальном проверенном cutoff активного short setup не было;
- OHLC не раскрывает истинный intrabar path, поэтому неоднозначность обрабатывается консервативно;
- один engine run обычно должен обрабатывать один symbol на экземпляр модуля.
