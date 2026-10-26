# Инженерный аудит Modular Strategy Core v0.3

## Результат

- regression tests v0.2: сохранены;
- новые архитектурные tests: добавлены;
- итог: **54 passed**;
- Python package compile: успешно;
- исходный код не содержит FVG, NY Range, Liquidity Sweep или другой strategy-specific логики.

## Проверено

### Module isolation

- missing dependencies отклоняются до обработки данных;
- feature dependency задаёт порядок модулей;
- same-bar downstream feature visibility детерминирована;
- неизвестные параметры и несовместимый API отклоняются;
- plugin загружается через registry/config без изменения core.

### Look-ahead и данные

- saved MarketView не расширяется будущими барами;
- Feature created_at в будущем запрещён;
- timestamp/open_time/close_time определены;
- warm-up блокирует преждевременный запуск;
- gap очищает незавершённый HTF и gap-sensitive features.

### Execution

- creation bar fill запрещён;
- market fill — следующий open;
- conservative limit default;
- stop gap-through uses worse open;
- bracket geometry revalidated on actual fill;
- partial fill оставляет remaining order;
- explicit scale-in строит weighted position;
- reduce-only partial exit не переворачивает позицию;
- GTD expiry и post-only rejection проверены;
- stop-limit path проверен.

### Audit

- setup → order → fill → position → close связаны;
- correlation/causation IDs формируются;
- event sequence append-only;
- run hashes воспроизводимы;
- JSON/CSV/JSONL export работает.

## Осознанные риски

1. OHLC не определяет внутрисвечной порядок; политика stop/target остаётся модельным допущением.
2. Partial fill model параметрическая и не заменяет order-book replay.
3. Один engine run пока обрабатывает один symbol.
4. Persistent EventStore/StateRepository и exchange adapters не реализованы production-кодом.
5. Margin, liquidation, funding и latency пока только contracts.
6. Module API остаётся draft до cross-audit реального внешнего plugin.

## Вердикт

v0.3 пригодно как архитектурная база конструктора и для начала разработки реальных независимых модулей. Следующий разумный шаг — не расширять core абстрактно, а подключить FVG plugin и провести reference-executor audit.

## Packaging

- wheel: `modular_strategy_core-0.3.0-py3-none-any.whl`;
- установка wheel в чистый venv без editable source: успешно;
- smoke backtest после установки: успешно;
- версия импортированного пакета: `0.3.0`.
