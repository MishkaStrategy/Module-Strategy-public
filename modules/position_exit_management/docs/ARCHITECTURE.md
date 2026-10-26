# ARCHITECTURE

`PositionExitManagementModule` — stateful StrategyModule, который получает закрытый `MarketView` и управляет только уже открытой net position.

## Границы

Внутри: time exit, partial exit, BE, trailing, stop update, orphan cancellation intent, state/checkpoint и typed status.
Снаружи: entry, initial stop/target, sizing, tick/step rounding, exchange calls, fill model, OCO, portfolio mutation, data loading.

## Data flow

`MarketView.current/position/pending_orders` → preflight cursor/stream validation → flat cleanup или position lifecycle → immutable `ModuleDecision` → Core execution layer.

## State

State schema 2 хранит config fingerprint, stream identity, event cursor, active position geometry, original risk, favorable extreme, partial stages, flags и bounded cancel attempts. Restore атомарен.

## Complexity

Основной stable no-op path — O(1), без обхода history. Collections bounded configuration caps. Feature status event-driven by default.

## Causality

Модуль читает только completed current bar и прошлое собственное state. Системное время, network и future bars не используются.
