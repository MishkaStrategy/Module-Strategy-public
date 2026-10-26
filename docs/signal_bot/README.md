# Modular Signal Bot

Production-oriented delivery layer for the Modular Strategy project. It consumes an already
confirmed, level-complete strategy decision; it never invents entries, stops or confidence and
never places orders.

## Boundary and flow

```text
Binance USDT-M closed REST klines ── MarketDataProvider
                                      ↓
registered SignalStrategy ── StrategyDecision (Decimal levels)
                                      ↓
closed-bar/tick/geometry/expiry validation ── stable signal_id
                                      ↓
SQLite lifecycle + idempotency ── chart job ── Telegram Notifier
```

All adapters are protocols. Binance, SQLite, Matplotlib and Telegram are replaceable without
changing `SignalEngine`. A cycle processes one instrument; the CLI catches configuration/startup
errors, while a scheduler should invoke each instrument independently so one failed pair cannot
stop the rest.

## Strategy contract

Implement `signal_bot.contracts.SignalStrategy`. `evaluate()` receives only validated closed
candle batches and returns `StrategyDecision`. A no-signal result is `StrategyDecision()`.
Confirmed results require side, timestamp, timeframe, entry type, exact entry, exact stop and may
include targets, invalidation, expiry and stable setup ID. Prices are `Decimal` and must already
match exchange tick size; the bot rejects rather than silently rounds them.

Configuration selects a strategy by registered ID, never by a Python import path. Built-ins and
the deterministic `modular_strategy.signal_strategies` entry-point group are accepted; duplicate
IDs, unknown IDs, version mismatches and a YAML `class` key fail startup.

`ModuleSignalAdapter` accepts only modules that explicitly declare `signal_delivery_ready = True`
and `portfolio_independent = True`, publish a compatible manifest and implement
`evaluate_delivery(context)`. Analytical or portfolio-sensitive modules are rejected: the bot
does not invent cash, equity, positions, entry, stop or target data.

## State and recovery

SQLite uses WAL, atomic transactions and a unique `signal_id`. Lifecycle:

```text
detected → validated → rendering → ready → sending → sent
                            ↘ send_failed → sending
```

Terminal cancellation/expiry transitions are supported. Candle cursors, Telegram update offsets,
preferences and signal IDs survive restart. A retry retains the same logical event. Delivery is
**persistent at-least-once with stable logical signal identity**. If Telegram accepts a message
but the process dies before SQLite records `sent`, a retry can produce a duplicate with the same
signal ID. Schema version 1 is created
idempotently; back up the SQLite database and its `-wal` file together using SQLite's online
backup API or while the service is stopped.

## Install and run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[signal-bot]'
cp .env.example .env
set -a; source .env; set +a
.venv/bin/modular-signal-bot --config configs/signal_bot.example.yaml --poll-seconds 15
```

The example is globally disabled (`enabled: false`) and its pair is disabled with a placeholder
strategy ID. Older configs without the top-level `enabled` field remain enabled by default. The
example exits successfully before checking Telegram secrets or initializing network clients; to
start the bot, explicitly set top-level `enabled: true` and enable at least one configured instrument.
Secrets belong only in environment variables. The default process polls continuously and handles
SIGINT/SIGTERM gracefully; use `--once` for an external scheduler. Never commit `.env` or the database.

At startup each enabled pair is checked against Binance Futures `exchangeInfo`; stale tick size,
step size, minimum price or notional blocks that pair. The daemon exposes `GET /health` and
Prometheus text at `GET /metrics` on `127.0.0.1:8080` by default. Non-loopback health binds require
`SIGNAL_BOT_HEALTH_TOKEN` and bearer authentication; see the deployment guide. Set `TELEGRAM_CHAT_ID`,
`TELEGRAM_ALLOWED_CHAT_IDS`, and `TELEGRAM_ADMIN_CHAT_IDS`; admins and the primary recipient must
be allowed. Unknown chats are silently ignored. Signals go only to the primary recipient, and only
it can change its notification preference. Commands are `/start`, `/status`, `/pairs`, `/last`,
`/notifications_on`, `/notifications_off` and admin-only `/health`. With `chart_enabled: false`,
Matplotlib is not called and Telegram receives a text message.

## Verification

```bash
MPLCONFIGDIR=/tmp/modular-signal-bot-mpl .venv/bin/pytest tests/signal_bot
.venv/bin/ruff check src/signal_bot tests/signal_bot
.venv/bin/mypy src/signal_bot
```

Tests cover deterministic ID, long/short geometry, tick alignment, expiry, future/open candles,
message formatting, SQLite restart/dedup, transient delivery retry, cleanup, pair isolation and
reference chart scenarios at very high and very low prices.

## Operations and troubleshooting

- `insufficient closed candles`: wait for warmup or inspect market connectivity/gaps.
- `open candle`: provider clock or exchange-close semantics are incorrect; signal is blocked.
- `aligned to tick_size`: fix strategy/instrument configuration; no silent normalization occurs.
- `send_failed`: the persistent outbox retries when `retry_at` is due, including after restart.
- Database corruption: stop the process, preserve the files, run `PRAGMA integrity_check`, restore
  the last backup, then backfill closed candles. Stable IDs prevent historical duplicates.

Known limits: polling uses REST rather than WebSocket; runtime metrics are process-local and
exchange rules refresh at startup rather than on a timer. Cancellation and expiry are persisted
and proactively delivered to Telegram. Databases created before UTC hardening can contain naive
local timestamps and require an operator-reviewed migration. See [DEPLOYMENT.md](DEPLOYMENT.md).
