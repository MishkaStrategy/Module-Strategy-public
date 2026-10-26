# Architecture and failure model

| Component | Owns | Must not own |
|---|---|---|
| `SignalStrategy` | pair-specific decision and exact levels | Telegram, persistence |
| `MarketDataProvider` | ordered, closed OHLCV recovery | strategy rules |
| `SignalEngine` | orchestration, validation, lifecycle | exchange/Telegram details |
| `SQLiteStateStore` | idempotency, cursor, transitions | business decisions |
| `ChartRenderer` | immutable chart artifact | delivery |
| `Notifier` | channel delivery and message ID | signal generation |

The project reuses `modular_core.Candle`, `Side`, `ModuleDecision` and manifests.
Delivery uses a stricter DTO because core analytical `Signal` intentionally does not promise an
entry or stop. This prevents accidental conflation of research facts and actionable alerts.

Failure domains are data, strategy, validation, rendering, delivery, storage and configuration.
Each pair is one `process()` boundary. Validation failures never register/send an event; registered
events advance only through explicit state transitions. A renderer or delivery exception records
`send_failed`, retains the same signal ID and cleans temporary files.

Before evaluating a pair, the orchestrator drains its due persistent outbox. Interrupted records
are reconstructed from the complete serialized signal, re-rendered from freshly fetched closed
candles and delivered with their original identity. Market polling, Telegram long polling and the
health server use independent controlled loops; SIGINT/SIGTERM preserves SQLite state.

Security assumptions: strategy selection uses only allowlisted registered IDs and version checks;
YAML is parsed with `safe_load`; unknown root/instrument keys fail closed; secrets come from the
environment; external data is reconstructed into validated immutable candles; there is no order
execution path.

Entry points come only from `modular_strategy.signal_strategies`, are ordered deterministically and
are rejected on duplicate IDs. Arbitrary YAML imports are unsupported.

Standalone mode evaluates a registered, portfolio-independent delivery strategy. Integrated mode
calls `SignalEngine.ingest()` with a Core-confirmed `TradeSignal` carrying `source="core"` and
contract version `1.0`; it never re-evaluates a strategy. Both paths use the same canonical fields
and stable ID algorithm. A future outbox or broker may feed this method without changing delivery.
