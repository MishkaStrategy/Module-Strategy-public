# Market Regime Module v1.2.0

Независимый аналитический модуль проекта **«Модульная Стратегия»**. Он классифицирует только завершённые целевые свечи по двум ортогональным измерениям:

- структура движения: `trend_up`, `trend_down`, `range`, `transition`;
- относительная волатильность: `low`, `normal`, `high`.

Модуль является только **feature-producer**. Он не открывает позиции, не рассчитывает размер сделки, не управляет ордерами и не содержит логики конкретного инструмента. Ядро взаимодействует с ним исключительно через Module API и immutable features.

## Публичный контракт

```python
from market_regime import MarketRegimeModule
```

Entry point: `modular_strategy.modules / market_regime`.

Манифест объявляет три feature:

| Feature | Назначение |
|---|---|
| `market_regime.current` | Полный снимок режима, волатильности, confidence и индикаторных метрик |
| `market_regime.trend` | Направление, подтверждённый и сырой режим, EMA/efficiency-метрики |
| `market_regime.volatility` | Подтверждённый и сырой уровень волатильности, ATR и относительный коэффициент |

Feature публикуются с `created_at`, равным времени закрытия целевой свечи. Они действуют один целевой интервал и помечены `invalidate_on_gap=True`.

Модуль не публикует orders, signals, setups, cancellations, close/update requests или portfolio mutations.

## Алгоритм

Модуль использует только завершённые свечи выбранного таймфрейма.

1. **Направление** определяется согласованностью:
   - разницы быстрой и медленной EMA относительно цены;
   - нормализованного наклона медленной EMA;
   - Kaufman Efficiency Ratio.
2. **Range** подтверждается низкой efficiency и малой EMA-разницей/наклоном.
3. Неоднозначное состояние классифицируется как `transition`.
4. **Волатильность** определяется отношением текущего Wilder ATR% к rolling baseline ATR%, рассчитанному только по уже доступным значениям.
5. Отдельный hysteresis требует заданное число последовательных подтверждений режима и волатильности, уменьшая односвечные переключения.
6. Финансово чувствительные промежуточные вычисления выполняются через `Decimal` с изолированным внутренним контекстом точности.

Будущие свечи, незакрытые HTF-свечи, будущие экстремумы и пересчёт прошлого результата не используются.

## Конфигурация

| Параметр | По умолчанию | Допустимое значение |
|---|---:|---|
| `timeframe` | `1h` | `<positive integer><s|m|h|d|w>`; canonical aliases поддерживаются |
| `fast_ema_period` | `20` | 2…10 000, меньше slow |
| `slow_ema_period` | `50` | 3…20 000 |
| `atr_period` | `14` | 2…10 000 |
| `volatility_baseline_period` | `50` | 5…50 000 |
| `efficiency_period` | `20` | 2…10 000 |
| `slope_lookback` | `5` | 1…10 000 |
| `trend_spread_threshold` | `0.001` | 0…1 |
| `trend_slope_threshold` | `0.00015` | 0…1 |
| `trend_efficiency_threshold` | `0.35` | range threshold < value ≤ 1 |
| `range_efficiency_threshold` | `0.22` | 0 ≤ value < trend threshold |
| `low_volatility_ratio` | `0.75` | 0…1, границы не включены |
| `high_volatility_ratio` | `1.35` | 1…1000, нижняя граница не включена |
| `regime_confirmation_bars` | `2` | 1…100 |
| `volatility_confirmation_bars` | `2` | 1…100 |
| `publish_diagnostics` | `true` | strict boolean |

Неизвестные поля и небезопасные coercions отклоняются. Decimal-параметры в resolved configuration хранятся каноническими строками. Численно равные `0.001`, `0.0010` и `1e-3` дают одинаковую семантическую подпись.

`publish_diagnostics` не входит в семантический fingerprint: переключение диагностики не меняет features и не делает checkpoint несовместимым.

## Подключение через YAML

```yaml
modules:
  - class: market_regime:MarketRegimeModule
    enabled: true
    parameters:
      timeframe: 1h
      fast_ema_period: 20
      slow_ema_period: 50
      atr_period: 14
      volatility_baseline_period: 50
      efficiency_period: 20
      slope_lookback: 5
      trend_spread_threshold: "0.001"
      trend_slope_threshold: "0.00015"
      trend_efficiency_threshold: "0.35"
      range_efficiency_threshold: "0.22"
      low_volatility_ratio: "0.75"
      high_volatility_ratio: "1.35"
      regime_confirmation_bars: 2
      volatility_confirmation_bars: 2
      publish_diagnostics: true
```

Ядро автоматически добавит целевой таймфрейм из `required_timeframes` и выстроит downstream-модули после Market Regime, если они объявят его features как required.

## Пример consumer-модуля

```python
regime = view.features.get(
    "market_regime.current",
    symbol=view.current.symbol,
    timeframe="1h",
)
if regime is not None and regime["regime"] == "trend_up":
    # Consumer принимает собственное решение; Market Regime не создаёт ордер.
    ...
```

Полный минимальный пример находится в `examples/consumer.py`; cross-module и clean-install smoke-сценарии — в `examples/integration_smoke.py` и `examples/installed_lifecycle_smoke.py`.

## Состояние и восстановление

Текущая state schema: `2`. Модуль читает schema `1` и `2`.

`snapshot_state()` возвращает полностью JSON-совместимый bounded payload. `restore_state()`:

- отклоняет несовместимую семантическую конфигурацию;
- отклоняет неизвестные/пропущенные поля;
- валидирует enum, числа, размеры rolling windows и причинную согласованность времени;
- валидирует symbol/timeframe/timestamp identity внутри fingerprints;
- применяет восстановление атомарно;
- мигрирует реальный historical schema-1 checkpoint;
- сохраняет идентичность последнего base/target события, hysteresis и health counters.

Непринятый corrupted/partial state не изменяет текущий runtime. После restart продолжение обязано давать тот же feature trace и final-state digest, что и непрерывный прогон.

## Безопасная обработка данных

- пустой MarketView возвращает структурированный skip;
- отсутствие целевого бара не вызывает исключение;
- незакрытая или будущая целевая свеча не используется;
- stale target не продвигает индикаторы;
- exact duplicate является идемпотентным;
- out-of-order и conflicting duplicate отклоняются информативным исключением;
- target validation транзакционна и не продвигает base cursor при ошибке;
- base gap и target-timeframe gap очищают indicator/hysteresis state;
- после gap модуль снова проходит warm-up;
- одна инстанция жёстко привязывается к одному symbol;
- разные инструменты обслуживаются независимыми экземплярами;
- rolling state и checkpoint не растут вместе со всей историей.

Typed skip reasons:

- `market_regime.empty_view`;
- `market_regime.no_target_bar`;
- `market_regime.not_ready`;
- `market_regime.data_gap_reset`;
- `market_regime.timeframe_gap_reset`;
- `market_regime.duplicate_bar`;
- `market_regime.stale_target`;
- `market_regime.incomplete_bar`.

## Установка и проверка

Из корня репозитория:

```bash
python -m pip install -e '.[dev]'
python -m pip install -e './modules/market_regime[dev]' --no-deps
python -m pip check
python -m compileall -q modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks modules/market_regime/examples
ruff check modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks modules/market_regime/examples
ruff format --check modules/market_regime/src modules/market_regime/tests modules/market_regime/benchmarks modules/market_regime/examples
(cd modules/market_regime && mypy --strict src/market_regime)
coverage run --source=market_regime -m pytest modules/market_regime/tests
coverage report --fail-under=90
python -m build modules/market_regime
```

Итоговая опубликованная suite: **151 тест**, **93% statement coverage** при gate 90%.

## Наблюдаемость

Диагностика содержит:

- текущие сырой и стабильный режимы;
- прогресс warm-up и недостающие компоненты;
- indicator metrics;
- `data_gap_resets`;
- `timeframe_gap_resets`;
- `duplicate_events`;
- `publications`.

`publish_diagnostics=false` отключает подробную диагностику, не меняя классификацию, feature IDs или restart continuity.

## Evidence и hardening

Для версии 1.2.0 выполнены:

- отдельный Error Discovery Audit;
- исправление семи подтверждённых дефектов;
- три круга полноты;
- ровно 30 содержательных hardening-кругов;
- 15 промежуточных аудитов;
- независимый clean-state финальный аудит;
- wheel/sdist/clean-install и deterministic restart проверки;
- 50k standard, 250k extended/restart и traced-memory workloads;
- CycloneDX SBOM и reviewed secret-scan evidence.

Документы находятся в `docs/`, машинные результаты — в `artifacts/`.

## Осознанные ограничения

- Одна инстанция обслуживает один symbol; портфельная оркестрация находится выше модуля.
- Режим является статистической классификацией, а не самостоятельным торговым сигналом.
- Значения по умолчанию являются безопасной базой, но параметры должны проходить per-instrument backtest/walk-forward отбор.
- Модуль работает по OHLCV и не использует стакан, funding, open interest или news features.
- Модуль не доказывает прибыльность стратегии и не является execution/live-компонентом.
- Production live-readiness зависит от общих live data/state/reconciliation adapters ядра.
- Python 3.12, full repository suite и тяжёлые GitHub CI jobs должны пройти на exact PR head до merge.

## Статус

Draft PR `#29` должен оставаться Draft до зелёного self-hosted CI и полного repository integration proof. Merge, stable tag и GitHub Release не выполняются без отдельной команды пользователя.
