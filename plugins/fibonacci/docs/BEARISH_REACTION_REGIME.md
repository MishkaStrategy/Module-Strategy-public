# Bearish reaction regime profile

## Назначение

4h/1d short-reaction setup не должен опираться только на all-history success rate. Поведение инструмента может ослабнуть, поэтому v0.4.0 сравнивает два хронологических непересекающихся окна terminal bearish-reaction observations:

```text
recent window: последние N наблюдений
prior window:  N наблюдений непосредственно перед recent
```

Default-параметры версионированы в `FibonacciConfig`:

```text
short_reaction_lookback = 40
short_reaction_minimum_samples = 8
short_reaction_minimum_success_rate = 0.65
short_reaction_minimum_streak = 3
short_regime_window_size = 12
short_regime_minimum_window_samples = 8
short_regime_minimum_success_rate = 0.65
short_regime_minimum_level_samples = 4
short_regime_minimum_level_success_rate = 0.65
short_regime_change_threshold = 0.15
```

## Классификация режима

- `improving` — recent rate выше prior rate минимум на `change_threshold`;
- `weakening` — recent rate ниже prior rate минимум на этот порог;
- `stable` — окна сопоставимы, но delta внутри пороговой полосы;
- `insufficient_data` — одно или оба окна пока недостаточны для сравнения.

Recent regime считается текущим надёжным, когда recent window имеет достаточно observations, recent success rate не ниже порога и классификация не `weakening`. `insufficient_data` само по себе не блокирует новый инструмент, когда recent window уже прошёл собственные sample/rate gates.

## Gate активного уровня

Статистика считается отдельно для `0.5` и `0.618` внутри recent window. Setup на `0.5` использует только recent observations уровня `0.5`; setup на `0.618` — только `0.618`. Активный уровень обязан независимо пройти `minimum_level_samples` и `minimum_level_success_rate`.

Это не позволяет сильной агрегированной статистике скрыть слабость конкретного уровня.

## Linked sequence и streak

Streak вычисляется только по непрерывному terminal suffix последнего `sequence_root_id`. Старый root не может перепрыгнуть через более новое несвязанное наблюдение. Root активной волны обязан совпадать с root этого непрерывного хвоста.

## Identity и fingerprint

Profile и regime обязаны совпадать с workflow по `symbol` и `timeframe`. `setup_id` — deterministic SHA-256 fingerprint, включающий:

- symbol/timeframe, wave/root/parent IDs;
- sequence number, anchors и endpoint revision;
- correction candle/time/level/ratio/price;
- historical sample/success/streak и thresholds;
- recent/prior window evidence;
- current-level sample/success evidence;
- regime classification, delta, окна и thresholds.

Изменение endpoint, correction, evidence или настройки создаёт новый аналитический факт, а не переиспользует старый feature ID.

## Point-in-time и scope

Scanner и walk-forward принимают frozen `FibonacciWorkflowSnapshot`. Каждый cutoff оценивается только по observations, присутствующим в этом snapshot; будущие outcomes не подмешиваются.

Модуль остаётся feature producer. Он не моделирует entry, stop, size, fees, slippage, funding и не создаёт orders/trades.
