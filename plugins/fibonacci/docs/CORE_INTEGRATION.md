# Core integration contract

`FibonacciModule` реализует публичные `StrategyModule`, `ModuleManifest`, `MarketView`, `ModuleDecision`, `Feature` и `FeatureDelete` из Core v0.4.

v0.4.0 добавляет regime-aware слой поверх проверенного адаптера Draft PR #243. Исходная Core boundary реализация сохраняет candle protocol, cursor barrier, tail-only processing и tuple/list-compatible checkpoint restore.

## Lifecycle

1. Core передаёт только закрытый point-in-time `MarketView`.
2. Адаптер обрабатывает только tail после собственного cursor.
3. При gap активные engines и gap-sensitive features очищаются; старое HTF-содержимое не пересканируется благодаря cursor barrier.
4. При изменении endpoint старая active feature удаляется и публикуется новая с другим revision-aware ID.
5. Completed feature публикуется один раз и не инвалидируется gap.
6. Checkpoint содержит engines, cursors, feature IDs, completed IDs и полный config fingerprint.
7. На 4h/1d short feature публикуется только после historical, regime, active-level и linked-root gates.

## `fibonacci.short_continuation_setup` schema v2

Payload включает `sequence_root_id`, `endpoint_revision`, historical sample/rate/streak, recent/prior sample/rates, regime classification/delta, current-level sample/rate и immutable evidence. Feature ID fingerprint-ит anchors, endpoint revision, correction, evidence, окна и thresholds.

## No execution boundary

`ModuleDecision.orders`, cancellations, position updates и closes всегда пусты. Scanner payload также явно содержит пустые `orders` и `trades`. Торговое решение принимает отдельный downstream consumer.
