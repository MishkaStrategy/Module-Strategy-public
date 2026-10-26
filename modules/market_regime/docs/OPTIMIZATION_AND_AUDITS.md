# Десять кругов оптимизации и пять промежуточных аудитов

## Круг №1 — корректность алгоритма

**Цель:** исключить look-ahead и математические неоднозначности.

Изменения:

- baseline ATR% считается до помещения текущего ATR% в rolling window;
- feature timestamp привязан к close time target candle;
- incomplete HTF блокируется;
- flat ATR=0 обработан без деления на ноль;
- confidence ограничен `[0, 1]`.

Тесты: trend/range/volatility, closed-bar timing, future-data invariance, flat market.

Результат: алгоритм детерминирован на закрытых point-in-time данных.

## Круг №2 — аудит №1

Области: архитектура, математика, временная логика, feature contract, gaps, тесты.

Замечания:

- base gap reset выполнялся после target lookup;
- target symbol/timeframe не проверялись явно;
- flat ATR требовал отдельной regression-защиты.

Исправления: reset перенесён в начало, добавлены boundary validations и regression tests.

Повторная проверка: существенных замечаний не осталось.

## Круг №3 — производительность

**Цель:** уменьшить стоимость последовательной обработки большой истории.

Изменения:

- EMA alpha, Decimal periods и timeframe delta вычисляются один раз;
- rolling sums/deques обновляются O(1);
- SHA fingerprint на hot path заменён точным canonical identity на `float.hex`;
- повтор уже обработанной HTF-свечи не пересчитывает индикаторы.

Измерение на 20 000 target bars в одинаковом окружении:

- исходная версия: `3.129145 s`;
- после precomputed constants: `3.098651 s`;
- после exact non-hash fingerprint: `2.970572 s`;
- улучшение относительно исходной версии: около **5.1%**.

Обнаруженная регрессия: первая версия fingerprint вызывала `.hex()` непосредственно на integer OHLC test values. Полный набор тестов поймал ошибку; значения теперь явно приводятся к `float`. Regression закреплён существующими int-valued fixtures.

## Круг №4 — аудит №2

Области: производительность, детерминированность, replay, gaps, границы свечей, multi-instrument.

Замечание: target duplicate identity недостаточна, потому что на каждом новом 5m base event видна прежняя закрытая 1h-свеча.

Исправление: введена независимая base-event identity. Теперь:

- тот же base event → replay counter + idempotent skip;
- новый base event, прежний target → дешёвый no-op;
- тот же timestamp с другим OHLCV → hard error.

Повторная проверка: base/HTF режим и параллельные symbol instances проходят.

## Круг №5 — устойчивость

**Цель:** безопасное восстановление и отказ.

Изменения:

- строгая state shape validation;
- JSON-native snapshot;
- transactional restore;
- validation non-finite/negative/invalid enum;
- consistency checks rolling windows и ATR initialization;
- informative errors для повреждённых checkpoint;
- gap resets очищают indicator/hysteresis state.

Результат: частично повреждённый checkpoint не изменяет рабочий модуль.

## Круг №6 — аудит №3

Области: restart, partial state, out-of-order, duplicate replay, failure paths.

Замечания:

- требовалась проверка причинной связи `last_base_open_time >= last_open_time`;
- base identity могла быть сохранена частично;
- restored duplicate не имел отдельного regression test.

Исправления: causal checks, pairwise identity validation и after-restore replay test.

Повторная проверка: snapshot → finish → start → restore → continue даёт тот же следующий feature.

## Круг №7 — качество интерфейса

**Цель:** минимальный и однозначный публичный API.

Изменения:

- feature keys и schema version вынесены в `contracts.py`;
- payload-схемы оформлены `TypedDict`;
- модуль не экспортирует внутренний mutable state;
- manifest и output используют единые constants;
- resolved Decimal parameters хранятся строками без float loss;
- неиспользуемый публичный skip reason удалён.

Результат: consumer зависит только от Module API и feature contract.

## Круг №8 — аудит №4

Области: заменяемость, совместимость с core, parameter schema, namespace, зависимости.

Замечание: исходное float-представление resolved thresholds могло терять различия высокой точности в run identity.

Исправление: constructor принимает numeric/string/Decimal, но resolved parameters всегда публикует exact canonical decimal strings; schema объявляет string.

Повторная проверка: registry принимает модуль, produced features уникальны, изменения ядра не требуются.

## Круг №9 — тесты и наблюдаемость

**Цель:** повысить объяснимость и диагностируемость.

Изменения:

- health counters: base gaps, target gaps, duplicates, publications;
- counters сохраняются в checkpoint;
- diagnostics включают warm-up missing requirements и metrics;
- добавлены негативные, parametrized, integration и regression tests;
- coverage gate установлен на 90%.

Результат: **80 tests**, **93% statement coverage**.

## Круг №10 — аудит №5

Области: полный предфинальный аудит архитектуры, алгоритма, данных, времени, performance, state, tests, docs, config и compatibility.

Замечания:

1. Численно равные Decimal-конфигурации `0.001` и `0.0010` имели разные textual signatures.
2. Неверный тип timeframe мог приводить к `AttributeError`, а не domain-specific config error.
3. README/пример конфигурации/инструкция consumer ещё не были полными.

Исправления:

- добавлена `canonical_decimal()` без exponent/trailing-zero ambiguity;
- timeframe type валидируется до normalization;
- добавлены regression tests на одинаковую signature;
- подготовлены README, architecture, configuration, lifecycle, audits, benchmark и changelog.

Повторная проверка:

- 80 tests passed;
- 93% coverage;
- compileall passed;
- no unresolved critical/high/medium findings.
