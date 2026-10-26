# Архитектура Modular Strategy Core v0.4

## 1. Слои

```text
Strategy configuration
        ↓
Module Registry + Manifest Planner
        ↓
Strategy Modules
        ↕ Feature Store / Setup Registry
Kernel Event Loop
        ↕
DataFeed / ExecutionAdapter / EventStore / Clock
        ↓
Audit + Run Manifest
```

Ядро зависит только от универсальных контрактов. Запрещены ветви по имени стратегии или модуля.

## 2. Module Manifest

`ModuleManifest` — декларативный разъём между модулем и ядром.

Он описывает требования, но не торговую логику. Новый модуль добавляется отдельным Python-пакетом, YAML-записью или entry point. Изменение ядра требуется только при появлении новой фундаментальной категории данных/исполнения, а не новой торговой идеи.

Планировщик:

1. валидирует API major;
2. проверяет parameters schema;
3. строит feature producer map;
4. обнаруживает missing dependency, duplicate producer и cycle;
5. выполняет stable topological sort;
6. формирует required timeframe plan.

## 3. Порядок обработки бара

```text
bar N закрыт
  ↓
Clock.advance(close_time)
  ↓
expire orders / SL-TP / scheduled exits
  ↓
fill ранее созданных orders
  ↓
order/fill/trade callbacks и setup auto-transitions
  ↓
base + HTF stores
  ↓
gap invalidation / feature expiry
  ↓
module A readiness + immutable MarketView
  ↓
features/setups/actions A применяются
  ↓
module B видит опубликованные A features на том же closed bar
  ↓
...
  ↓
equity mark
```

Entry/exit request, созданный модулем на bar N, не исполняется раньше bar N+1.

## 4. Feature Store

Feature является immutable point-in-time fact. Store принадлежит ядру; модули видят только `FeatureView`.

Инварианты:

- key namespaced;
- source совпадает с manifest owner;
- key объявлен в `produced_features`;
- created_at не находится в будущем;
- old MarketView не получает новые features;
- gap-sensitive feature удаляется при gap;
- expired feature не видна после `valid_until`.

## 5. Setup Registry

Registry хранит формальное состояние каждого setup и проверяет переходы. Execution events автоматически продолжают lifecycle, если у `OrderRequest` указан `setup_id`.

Связи:

```text
parent setup
  └─ child confirmations
setup
  ├─ order_ids
  └─ position_id
```

Terminal setup нельзя повторно активировать.

## 6. MarketView

Модуль получает:

- O(1) bounded `ReadOnlySeries` base bars;
- closed HTF bars;
- cash/equity;
- frozen position/order snapshots;
- FeatureView;
- ReadinessView;
- SessionView;
- gap flag;
- optional order-book/trade-tape extension slots.

Модуль не получает mutable broker, portfolio или stores.

## 7. Orders, fills and position ledger

`Order` хранит requested, filled, remaining quantity и average fill price. Один order может породить несколько fills.

```text
Order 4 BTC
  ├─ Fill 2 BTC
  ├─ Fill 1 BTC
  └─ Fill 1 BTC
        ↓
Net Position 4 BTC
```

Reduce-only fills уменьшают позицию. Каждая закрытая часть создаёт trade slice; setup становится CLOSED только после полного закрытия позиции.

Scale-in разрешается отдельной глобальной политикой, не модулем напрямую.

## 8. Exchange Rules

Все exchange-specific ограничения сосредоточены в `InstrumentSpec`/`ExchangeRulesAdapter`. Они не должны копироваться в стратегии.

Нормализация выполняется до создания order и повторная risk validation — на фактическом fill.

## 9. Time model

`Candle.timestamp` обозначает open time. Бар считается доступным модулю только после `close_time`.

Calendar aggregation использует local wall-clock bucket boundaries в заданной IANA timezone. Это позволяет корректно задавать UTC exchange buckets и DST-aware session buckets.

## 10. Event model

Every event is append-only and versioned. Sequence strictly increases inside a run.

Causality example:

```text
setup.confirmed evt-10
  ↓ causation_id
order.submitted evt-11
  ↓
fill.recorded evt-12
  ↓
order.filled evt-13
  ↓
position.closed evt-20
```

EventStore можно заменить persistent implementation без изменения модулей.

## 11. Reproducibility

`RunManifest` фиксирует kernel version, module manifests, resolved module parameters, data/config/module hashes, symbol, base timeframe и bar count. Run identity не зависит от времени запуска.

## 12. Adapter boundaries

Backtest использует:

- HistoricalDataFeed;
- BacktestExecutionAdapter;
- InMemoryEventStore;
- SimulationClock.

Paper/live реализации обязаны поддерживать те же внешние DTO/event semantics. Restart recovery строится через versioned module checkpoints, StateRepository и replay EventStore.

## 13. Extension contracts

Ядро заранее содержит Protocol/ABC для:

- funding/borrow;
- margin/liquidation;
- latency/spread/slippage/liquidity;
- order book/trade tape;
- paper/live execution;
- state persistence;
- optimization/Monte Carlo/distributed research.

Они изолированы от текущего backtest path.

## 14. Инварианты v0.4

1. Future bars/features недоступны.
2. Новый модуль не требует правки ядра.
3. Dependency graph проверяется до run.
4. Undeclared feature отклоняется.
5. Invalid setup transition отклоняется.
6. Order не исполняется на creation bar.
7. Partial fill сохраняет remaining order.
8. Reduce-only не переворачивает позицию.
9. Exchange rounding не увеличивает риск намеренно.
10. Gap не публикует неполный HTF bucket.
11. Event sequence и run hashes воспроизводимы.
12. Strategy-specific concepts отсутствуют в core.

## 15. Заморозка API

API v1 замораживается только после:

- интеграции FVG producer;
- интеграции setup/entry consumer;
- cross-audit сделок независимым executor;
- проверки gap/DST/rounding на реальных данных;
- migration test внешнего plugin package.


## 16. Checkpoint model v0.4

Каждый модуль имеет стандартные `snapshot_state()` и `restore_state()`. Checkpoint содержит версию ядра, run/data/config/module hashes, последний обработанный бар и состояние каждого модуля с собственной версией схемы. Восстановление запрещается при несовпадении имени, версии модуля или state schema.

## 17. Durable replay v0.4

`JsonLinesEventStore` хранит append-only поток. `verify_event_stream` проверяет уникальность ID, порядок sequence, единый run, монотонное время и отсутствие ссылок causation на будущие/неизвестные события.
