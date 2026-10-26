# Round 19 — restart state snapshot boundary

Added a universal immutable `ExecutionStateSnapshot` and provider protocol. The backtest broker can expose pending order and net-position state for recovery comparison without granting mutation or performing exchange actions.

This is a reconciliation foundation, not a paper/live implementation.
