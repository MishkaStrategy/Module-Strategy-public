# Real Core integration evidence

Дата проверки: 2026-07-28.

Fibonacci v0.3.0 проверен против точного `src/modular_core` из merge commit Draft PR #243, построенного поверх `main` commit `996671bce0c2f8d2670791918476631fd30efd4f`.

## Контрактные проверки

- реальный `ModuleRegistry.validate_and_plan` принимает manifest;
- реальный `BacktestEngine` публикует `fibonacci.active_projection` и `fibonacci.wave_completed`;
- `ModuleDecision` не содержит orders/cancellations/position actions;
- реальный `KernelCheckpoint.module_states` восстанавливается после Core `deep_freeze` (`MappingProxyType` и tuple);
- повторный запуск после restore сохраняет endpoint revision и completed-wave identities.

Результат: **118 tests passed**, combined branch-aware coverage **90%**.

## BTCUSDT 15m — 5 000 баров

- период: 2026-05-28 05:00 UTC — 2026-07-19 06:45 UTC;
- 25 завершённых волн;
- 341 публикация active projection;
- 340 удалений старых ревизий;
- 0 ошибок, 0 ордеров, 0 сделок;
- скорость: около 1 374 бар/с.

## Restart equivalence — 10 000 баров

Сравнены:

1. непрерывный прогон 10 000 баров;
2. 5 000 баров → Core checkpoint → новый экземпляр модуля → restore → ещё 5 000 баров.

Результат:

- финальные module states идентичны;
- последовательность 61 completed-wave feature ID идентична;
- 0 ошибок, 0 ордеров, 0 сделок.

## Автоматическая HTF-агрегация — 20 000 базовых 15m баров

Core самостоятельно сформировал 4h/1d series. Fibonacci опубликовал:

- 126 active projection на 4h;
- 12 completed wave на 4h;
- 15 active projection на 1d;
- 0 ошибок, 0 ордеров, 0 сделок.

Строгий `fibonacci.short_continuation_setup` на выбранном cutoff не активировался. Это корректный отрицательный аналитический результат, а не failure.

## Важное найденное исправление

Core глубоко замораживает checkpoint payload и превращает JSON-массивы в tuple. Restore Fibonacci первоначально принимал только list. Реализация исправлена: все вложенные sequence-state поля принимают list/tuple, после чего restart-equivalence прошёл.

Полные JSON-отчёты находятся в `plugins/fibonacci/reports/`.
