# Results Aggregator package boundary

## Purpose

Research Runner publishes completed Backtest Engine evidence to the separate Results
Aggregator & Experiment Registry contract. It does not register database entities itself and
does not recalculate any trading metric.

## Source verification

`LocalResultsAggregatorPackageSink` accepts only a completed `BacktestRunReference`. Before
publication it verifies:

- a real, non-symlink Backtest Engine output directory;
- the authoritative `artifact-manifest.json` schema and engine run identifier;
- the exact supported Core and application artifact set;
- every SHA-256 declared by the Engine manifest;
- clean Git provenance and a dependency-lock SHA-256;
- experiment, strategy, configuration, dataset, stage, scenario, seed, and metric-schema
  lineage across the specification, experiment manifest, job, and result envelope;
- configured per-file and total package size limits.

Unknown, missing, extra, linked, oversized, or checksum-mismatched artifacts fail closed.

## Separate content-addressed package

The Engine output directory is never edited. The sink copies the verified evidence into a
staging directory below the configured package root, verifies every copied byte, writes the
Results Aggregator v1 `artifact-package.json`, fsyncs the evidence, and atomically renames the
staging directory to its content-derived package ID.

The package includes:

- Core run, metrics, journal, trades, equity, and event artifacts;
- Backtest application provenance and extended metrics;
- data-validation and restart evidence;
- optional replay checkpoint evidence;
- the original Backtest Engine artifact manifest;
- repository, dependency, dataset, split, scenario, and seed lineage.

The package ID derives from the complete manifest payload before its own ID and integrity
checksum are added. Re-publication is idempotent only when the manifest and every artifact
match exactly; any existing-content conflict is rejected.

## Results Aggregator validation

The emitted document uses schema ID
`modular-results-registry.artifact-package`, schema version `1`, and the canonical JSON/checksum
semantics implemented by canonical Results branch `research/results-aggregator` at
`b33be3492c670ee282f0be5c08fc5236ff772894`. Exact Research Runner candidate CI checks the
package with the authoritative `validate_artifact_package` implementation and producer profile.
