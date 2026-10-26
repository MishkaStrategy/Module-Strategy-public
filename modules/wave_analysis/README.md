# Wave Structure Analysis 0.2.0rc2

`WaveAnalysisModule` is a deterministic, non-repainting analytical feature producer for Modular Strategy Core `0.4.x` and Module API `1.x`.

It consumes only completed OHLCV candles and produces objective point-in-time market structure. It never emits orders, trading signals, setup events, position updates, stops, targets, or portfolio actions.

## Capabilities

- fractal, percentage ZigZag, ATR ZigZag, and hybrid adaptive pivot detection;
- explicit separation of extremum time (`pivot_time`) and availability time (`confirmed_at`);
- immutable confirmed pivots and independently revisioned provisional pivots;
- directed wave legs with Decimal-based geometry, ATR/volume/path metrics, MAE/MFE, slope, velocity, acceleration, confidence, and reason codes;
- HH/HL/LH/LL/equal/range labels and bullish/bearish/range/transition structure;
- impulse, correction, expansion, contraction, exhaustion, range-leg, and unclassified categories;
- retracement, extension, and Fibonacci measurements without entry/exit semantics;
- optional bounded Elliott-like hypotheses with hard-rule invalidation separated from guidelines;
- completed-bar multi-timeframe gating;
- active-segment isolation across data gaps and mandatory post-gap recovery;
- strict duplicate, conflicting-duplicate, out-of-order, and corrupted-state handling;
- bounded state, bounded Feature Store snapshots, deterministic checkpoint/restart, and thread-safe instance operations.

## Installation

```bash
python -m pip install modular_strategy_wave_analysis-0.2.0rc2-py3-none-any.whl
```

The Core package must satisfy `modular-strategy-core>=0.4,<0.5`.

## Configuration example

```yaml
modules:
  - plugin: wave_analysis
    enabled: true
    parameters:
      timeframes: [15m, 1h, 4h]
      primary_timeframe: 15m
      pivot_method: hybrid_adaptive
      reversal_threshold: "0.01"
      atr_period: 14
      atr_multiplier: "2"
      feature_snapshot_limit: 32
      recovery_bars: 0
      elliott_enabled: false
```

`recovery_bars: 0` means “use the detector warm-up requirement”.

## Module discovery

```python
from modular_core import ModuleRegistry

registry = ModuleRegistry()
registry.discover_entry_points()
module = registry.load_named("wave_analysis", {"timeframes": ["15m"]})
```

## Local validation for RC2

```bash
pytest -q
coverage run --source=wave_analysis -m pytest -q
coverage report --fail-under=95
ruff check src tests benchmarks
mypy
python -m compileall -q src tests benchmarks
python -m build
```

RC2 local result: 159 tests passed, 95% statement coverage, Ruff clean, strict mypy clean, wheel/sdist build and clean-install smoke passed.

## Important limitation

Wave and Elliott-like labels are analytical descriptions, not evidence of predictive edge. Profitability, robustness, execution behavior, and live suitability require separate strategy-level backtest, walk-forward, paper-trading, and operational validation.
