# HANDOFF — Session Range 0.2.0

## Component

- Name: Session Range
- Purpose: deterministic, DST-aware, gap-safe OHLCV range producer for configurable local sessions
- Status: `LOCAL RELEASE CANDIDATE — READY FOR CODEX INTEGRATION`
- Recommended repository target: `modules/session_range/`
- Public import: `modular_session_range`
- Main class: `modular_session_range.SessionRangeModule`
- Entry point: `modular_strategy.modules / session_range`
- Core dependency: `modular-strategy-core>=0.4,<0.5`
- Python: `>=3.12`

## Manifest

```text
name=session_range
version=0.2.0
api_version=1.0
required_timeframes=()
required_features=()
produced_features=(session_range.active, session_range.completed)
warmup_bars={}
skip_until_ready=False
state_schema_version=2
```

## Inputs

- `MarketView.current`: one completed `Candle` with positive interval, aware timestamps, symbol and timeframe.
- `MarketView.data_gap`: gap signal supplied by the engine.
- `MarketView.features`: optional Feature Store view used only to avoid re-emitting a restored completed feature already present.

No system time, files, network, order state, portfolio mutation, or future bars are read.

## Outputs

`ModuleDecision` containing immutable feature tuples, structured skip reason strings, and diagnostics. It never emits orders or position/risk decisions.

Feature keys: `session_range.active`, `session_range.completed`.

## Configuration

See `examples/config.example.yaml`. Configuration is strict; unknown keys and wrong scalar types are rejected. Sessions are local half-open intervals `[start, end)`.

## State

Schema 2, deterministic JSON-compatible mapping. Collections: active accumulators, last bar cursors, terminal tokens, last completed features, plus module/config identity. Schema 1 is migrated under strict checks.

## Installation and testing

```bash
python -m pip install ./modules/session_range
python -m pytest -q modules/session_range/tests
python -m coverage run --source=modules/session_range/src/modular_session_range -m pytest modules/session_range/tests
python -m coverage report --fail-under=92
python -m ruff check modules/session_range/src modules/session_range/tests modules/session_range/benchmarks
MYPYPATH=modules/session_range/typings python -m mypy --strict modules/session_range/src/modular_session_range
python -m build modules/session_range
```

The minimal `typings/modular_core/__init__.pyi` in evidence is only a local static-analysis harness. When integrating into the full repository, prefer `MYPYPATH=src` and the real Core source, as the repository CI already does.

## File operations

Replace the existing Session Range package implementation, tests, README, pyproject, benchmark, and configuration example with the files listed in `FILES_TO_ADD_OR_REPLACE.txt`. Do not modify `src/modular_core`, other modules, root feature keys, or dependency planner behavior.

## Integration order

1. Apply/copy Session Range files only.
2. Install root Core 0.4.x.
3. Install Session Range editable or from wheel.
4. Run Session Range unit/regression tests.
5. Run the real registry/dependency planner integration tests.
6. Run repository-wide tests and cross-module smoke.
7. Confirm downstream Breakout–Return (or any consumer) declares `session_range.completed`, not an exact producer implementation dependency.

## Neighbor expectations

- Upstream: completed base-timeframe candles from the engine.
- Downstream: analytical setup modules may consume `session_range.completed` through Feature Store/dependency planner.
- No dependency on FVG, Liquidity Sweep, Market Regime, Entry, Risk, Protection, Exit, or execution modules.

## Core Change Requests

None. See `CORE_CHANGE_REQUESTS.md`.

## Limitations

See `docs/KNOWN_LIMITATIONS.md`.
