# How to add a trading pair

1. Implement or select a `SignalStrategy` that returns exact `Decimal` entry/stop levels.
2. Install its package in the bot environment. Do not point configuration at arbitrary files.
3. Add a YAML entry under `instruments` with the Binance Futures symbol, market, tick/step size,
   exact strategy import path and pinned version.
4. Start with `enabled: false`; run contract and historical closed-bar tests.
5. Verify LONG, SHORT, expiry, reconnect/restart dedup and both price extremes in a paper channel.
6. Set `enabled: true` and restart the supervised polling cycle.

No Python core edit is needed. To replace a strategy, change only its installed package, import
path/version and parameters, then repeat contract tests. A version mismatch aborts before signals
are sent.
