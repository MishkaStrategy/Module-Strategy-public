# Entry Model 0.2.0 — Handoff

## Component

- Name: Entry Model
- Purpose: transform a canonical confirmed setup into a deterministic entry plan.
- Status: `LOCAL RELEASE CANDIDATE — READY FOR CODEX INTEGRATION`
- Recommended repository target: `modules/entry_model/`
- Public import: `modular_modules.entry_model`
- Entry point: `modular_strategy.modules:entry_model`
- Core compatibility: Modular Strategy Core 0.4.x, Module API 1.0

## Manifest

- module name: `entry_model`
- module version: `0.2.0`
- API version: `1.0`
- state schema: `2`
- required feature: `setup.confirmed`
- produced features: `entry_model.plan`, `entry_model.rejection`
- required timeframes: none
- skip-until-ready: false

## Inputs and outputs

Input is Feature key `setup.confirmed`, schema `1`. Output is either a schema-2 `entry_model.plan` or schema-2 `entry_model.rejection`. Exact schemas and reason codes are in `INTEGRATION_CONTRACT.md`.

## Configuration

- `default_entry_kind`: string enum, default `limit_retracement`
- `retracement_fraction`: exact decimal string, default `0.382`, range `(0, 1)`
- `ttl_bars`: integer, default 36, range `[1, 1_000_000]`
- `honor_setup_hint`: boolean, default true
- `post_only_limits`: boolean, default true
- `stop_limit_offset_bps`: exact decimal string, default `0`, range `[0, 10000)`
- `max_setup_age_seconds`: exact decimal string, default `0`, range `>=0`
- `max_processed_setups`: integer, default 10,000, range `[1, 1_000_000]`
- `allowed_entry_kinds`: nonempty entry-kind list containing the default

## State and restart

State schema 2 stores configuration fingerprint, bound symbol/timeframe, bar cursor/signature, cumulative counters and bounded processed/source-feature records. Restore is atomic and rejects corrupt, partial, oversized, duplicate, unknown-field, wrong-schema and wrong-config state.

Do not silently migrate schema-1 state. Start clean or implement an explicit migration outside this module’s release candidate.

## Integration order

1. Install/copy the standalone package under `modules/entry_model`.
2. Ensure a canonical producer of `setup.confirmed` is registered.
3. Let the dependency planner order producer → Entry Model → downstream consumers.
4. Downstream protection/risk/execution modules should consume `entry_model.plan` through `EntryPlan.from_feature_value`.

## Installation and verification

```bash
python -m pip install -e .
python -m pip install -e 'modules/entry_model[dev]'
pytest -q modules/entry_model/tests
coverage run --source=modular_modules.entry_model -m pytest -q modules/entry_model/tests
coverage report --fail-under=90
ruff check modules/entry_model/src modules/entry_model/tests modules/entry_model/benchmarks modules/entry_model/scripts
mypy --strict modules/entry_model/src/modular_modules/entry_model
python -m build modules/entry_model
```

## Files

Add the package source, tests, benchmark, clean smoke, examples and documents listed in `FILES_MANIFEST.json`. `FILES_TO_ADD_OR_REPLACE.txt` provides explicit mapping.

## Files that must not be changed by this integration

- Core `StrategyModule`, `ModuleManifest`, `MarketView`, `ModuleDecision` and Feature Store contracts;
- dependency planner behavior;
- other module source trees;
- strategy-specific upstream feature keys;
- root application/execution code unless separately integrated.

## Neighbor expectations

Upstream: any module or adapter producing the canonical `setup.confirmed` schema. The current strategy-specific `breakout_return.confirmed` key is not consumed directly.

Downstream: Protection/Target, Risk/Sizing or an OrderIntent assembler consuming the typed plan. Entry Model itself does not place orders.

## Known limitations

- one mutable instance per symbol/timeframe stream;
- no concurrent mutation guarantee for one instance;
- bounded idempotency retention permits very old evicted events to be processed if upstream republishes them;
- TTL is a planned bar lifetime, not exchange acknowledgement;
- exchange rounding, marketability and execution are downstream;
- Python 3.12 runtime was not available locally; Python 3.13.5 passed and static tools target 3.12.

## Core changes

None. See `CORE_CHANGE_REQUESTS.md`.

GitHub CI: NOT RUN — intentionally deferred until local integration on July 25, 2026.
