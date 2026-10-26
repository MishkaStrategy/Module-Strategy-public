# Funding Runner and Publication V3

This slice wires the accepted registered-funding adapter and funding-aware broker into the Backtest application runner while preserving the existing Core execution API and Core run identity.

It is a bounded GAP-008 / Issue #376 slice. Funding remains a programmatic opt-in (`run_backtest(..., funding_source=...)`) in V3; YAML/CLI configuration and mark/index valuation remain later work.

## One execution authority

`run_backtest` accepts an optional immutable `RegisteredFundingSource`.

When it is present, every engine factory used by the runner receives one `FundingAwareBrokerSimulator` factory through the existing Core `execution_factory` hook. This applies to:

- normal scalable single-pass execution;
- registered streaming checkpoint replay;
- registered materialized prefix replay;
- nonregistered direct runtime snapshot restart.

No second engine, broker, portfolio, journal or checkpoint store is introduced.

When funding is absent, the Core default `BrokerSimulator` path remains unchanged.

## Core run ID vs application run ID

The deterministic Core run manifest remains authoritative for strategy/config/candle execution identity. V3 does not rewrite `run.json.manifest.run_id`.

A funding-enabled application has an additional identity derived from:

- application identity schema version;
- Core `run_id`;
- funding runtime model version;
- immutable registered funding source identity SHA-256.

The resulting application ID is filesystem-safe and is used as the output directory name. Therefore the same Core run with a different funding owner dataset cannot collide with either:

- the funding-disabled baseline directory; or
- a run using another immutable funding source.

Funding-disabled runs keep the previous output directory name exactly equal to the Core run ID.

## Artifact manifest compatibility

`artifact-manifest.json` keeps its existing `run_id` field equal to `run.json.manifest.run_id` so existing `inspect_result` and result comparison contracts remain valid.

For a funding-enabled run only, the manifest adds `application_run_id`. `verify_artifact_manifest` validates and preserves this optional field. `ResultSnapshot.application_run_id` returns it when present and otherwise falls back to the Core run ID.

The public `compare_results()` output shape is unchanged.

## Funding runtime evidence

After the authoritative journal is exported, V3 scans the complete `events.jsonl` rather than a retained in-memory window.

Every `portfolio.funding` event must prove:

- source `backtest_funding`;
- accepted funding runtime model version;
- immutable funding source identity SHA-256;
- valid nonnegative ordinal;
- deterministic event key;
- finite exact-text cashflow.

Journaled funding ordinals must be unique, chronological and contiguous from the first active-runtime ordinal. A first ordinal greater than zero is allowed because V2 may consume owner events before the first processed candle as necessarily-flat pre-run events without writing timestamps older than `run.started`.

The runner writes `funding-runtime.json` containing only sanitized evidence:

- application run ID;
- model version;
- immutable owner source identity and owner-safe identity payload;
- journaled settlement count;
- first/last journaled ordinals;
- cumulative journaled Decimal cashflow;
- SHA-256 over ordered settlement event keys.

Storage URIs, raw source URLs and credentials are not included.

`application-run.json` includes the application/Core identities, funding model/source identity and the same deterministic funding evidence summary.

## Real direct runtime snapshot acceptance

V3 tests use the actual Backtest `run_with_runtime_snapshot_restart` path with a funding-aware scalable engine.

The checkpoint is taken after the initial flat funding boundary and before a later funding boundary while a position is open. The existing custom runtime snapshot serializes the authoritative broker inside `ScalableRuntimeState`; after restore, the remaining funding event is settled exactly once.

The restarted final `_result_digest` must equal an uninterrupted run using the same funding source, engine configuration and candles. The funding journal event sequence must also match.

This test specifically exercises the production custom snapshot pickler/`StreamingEventStore` reducer rather than an artificial plain pickle of the normal in-memory journal.

## Publication idempotence and conflicts

A repeated run with identical Core and funding identities resolves to the same application directory. Existing manifest verification decides whether the publication is byte-identical and reusable.

A different immutable funding source creates a different application directory before publication, so it cannot overwrite or conflict with the baseline or another funding owner dataset.

## V3 non-goals

V3 does not yet:

- add a YAML/CLI funding configuration contract;
- automatically select a funding dataset from the candle dataset;
- introduce registered mark/index price readers;
- change mark-vs-last valuation or liquidation semantics;
- publish futures assumptions into Results Aggregator / Research Runner contracts;
- run canonical full-corpus funding-disabled vs funding-enabled A/B evidence;
- close Issue #376.

The next safe GAP-008 slice should add the registered mark/index reader dependency and/or a typed operator-facing funding configuration contract, then continue toward explicit valuation scenarios and full-corpus A/B acceptance.
