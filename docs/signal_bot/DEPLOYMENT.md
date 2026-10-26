# Deployment and recovery

The service only emits paper-signal notifications. It has no exchange credentials, order client,
quantity calculation or order execution path.

## Required configuration

The top-level YAML `enabled` flag controls process activation. It defaults to `true` when omitted
for compatibility with existing configurations. The checked-in example sets it to `false`; a
disabled config exits successfully before Telegram secrets are required or network clients are
initialized. To run the service, explicitly set `enabled: true` and enable at least one instrument.

Set `TELEGRAM_BOT_TOKEN`, primary `TELEGRAM_CHAT_ID`, `TELEGRAM_ALLOWED_CHAT_IDS`, and
`TELEGRAM_ADMIN_CHAT_IDS`. The primary and every admin must occur in the allowlist. Keep `.env`
outside version control. Select an allowlisted strategy ID in YAML and enable instruments only
after exchange-rule validation succeeds. `notifications.chart_enabled: false` is text-only: no
chart or temporary PNG is created.

## Process and probes

Run `modular-signal-bot` under systemd, launchd, Docker, or another process supervisor that sends
SIGTERM and waits for graceful shutdown. Health binds to `127.0.0.1` by default; `localhost` is
normalized to `127.0.0.1` because the server uses IPv4. Keep this default for the Linux/VDS profile.

To scrape health remotely or from a separate container, set `SIGNAL_BOT_HEALTH_TOKEN` to a random
secret of at least 32 ASCII characters and configure `--health-host` to the required IPv4 address,
hostname, or wildcard. Every `/health` and `/metrics` request then requires
`Authorization: Bearer <token>`. Keep the token out of command-line arguments and logs. For remote
access, use a TLS-terminating authenticated reverse proxy and forward the backend token only over
loopback or an isolated private network; never expose the plain-HTTP backend to untrusted networks.
IPv6 health binds are not supported.

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/metrics
```

Health and metrics contain no token, chat ID or trade levels. A port conflict is a startup error.

## SQLite backup and restart

SQLite is configured by the YAML key `state.database`. It defaults to `data/signal_bot.sqlite3`
relative to the configuration file's directory; the loader resolves that path before opening the
store. The database uses WAL plus a 30-second busy timeout. Stop the service before a file copy and
copy the database together with existing `-wal` and `-shm` files, or use SQLite's online backup API.
Restore all files as one set and run `PRAGMA integrity_check` before restart. Do not copy only the
main file while the process is active. See the [configuration loader](../../src/signal_bot/config.py).

Signals, transitions, candle cursors, preferences and Telegram update offsets persist. Malformed
or replayed command updates advance/observe the durable offset and are not executed again. Signal
delivery is at-least-once, not exactly-once: a crash after Telegram accepts a message but before
the `sent` commit can produce a duplicate with the same stable signal ID.

Databases written before UTC hardening may contain naive local timestamps. Preserve a backup and
perform an operator-reviewed migration; the service writes all new timestamps as aware UTC.

## Release verification

Fast CI runs Python 3.12/3.13 on the labelled VDS runner. The manual heavy audit must be dispatched
with the exact commit SHA on the labelled Mac backtest runner. Never use production Telegram or
Binance credentials in either workflow.
