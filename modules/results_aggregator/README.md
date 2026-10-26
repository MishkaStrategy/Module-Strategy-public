# Results Aggregator & Experiment Registry

Status: **DRAFT FEATURE BRANCH — NOT MERGED / NOT RELEASE-READY**.

This feature-branch candidate is developed against the verified
`modular-strategy-core==0.4.0` contract associated with integration commit
`5e03b933207a99940244634f49a756d27888a5c3`. It is tracked remotely on
`research/results-aggregator` in Draft PR #94. The full source tree is being
synchronized into `modules/results_aggregator/`; the branch is not merged or
release-ready.

The candidate has passed its isolated local gates, but it is not release-ready.
Full-repository cross-module integration, final Fast CI, the Heavy Aggregation
Audit and the project release gate remain pending.

## Hard boundaries

The package:

- does not run `BacktestEngine` or a strategy;
- does not calculate or redefine official Core trading metrics;
- does not generate signals, orders, fills, stops, targets or sizing;
- accepts exported Core/research artifacts as immutable producer evidence;
- never treats missing metrics/evidence as zero or PASS;
- never chooses or promotes a production/live candidate;
- performs no network access and executes no artifact code.

## Implemented candidate capabilities

- typed immutable entities and deterministic SHA-256 identifiers;
- trusted package intake, exact inventory and fail-closed resource limits;
- content-addressed immutable artifact storage;
- atomic SQLite registration with FK integrity and WAL;
- idempotent duplicate import and committed conflict quarantine;
- database-enforced append-only entities, relations, migration history, audit events and registry annotations;
- immutable Metric Definition Registry with formula/unit/precision/aggregation/missing-policy/owner lineage;
- strict Core 0.4 metric-snapshot adapter without recalculation;
- compatibility gate and baseline/candidate deltas;
- streaming instrument/period/side/regime/module/setup slices;
- constrained deterministic Pareto ranking;
- stability, statistical-strength and evidence-completeness findings;
- deterministic Strategy Audit evidence package and reports;
- read-only query API with whitelisted filters and pagination;
- no-clobber consistent backup/restore, revision-ordered rebuild v4 and schema migration;
- structured redacted JSONL logging and bounded operational metrics;
- deterministic wheel build, bundled machine-readable schemas, static security scan and local performance evidence;
- revision-aware derived-view cache that is never a source of truth;
- fail-closed resource, retention and query-timeout policies.

## Local commands

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./modular_results_registry-0.1.0.dev0-py3-none-any.whl
results-registry --help
```

Development verification without network access:

```bash
PYTHONPATH=src coverage run --branch -m pytest
coverage report --show-missing --fail-under=90
python -m compileall -q src tests
python tools/security_scan.py src tests tools pyproject.toml README.md BOUNDARY.md
```

## Bundled schemas

The wheel includes the same seventeen JSON Schema documents published in `schemas/`. They
are available through `list_schema_names()`, `read_schema()` and
`schema_hash()`. Tests prevent repository and wheel copies from drifting.

## Documentation

- [Final local candidate audit](docs/FINAL_CANDIDATE_AUDIT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Artifact package](docs/ARTIFACT_PACKAGE_REFERENCE.md)
- [Registry contracts](docs/REGISTRY_REFERENCE.md)
- [Integrity and lineage](docs/INTEGRITY_AND_LINEAGE.md)
- [Immutable artifact policy](docs/IMMUTABLE_ARTIFACT_POLICY.md)
- [Comparison and slices](docs/COMPARISON_AND_SLICES.md)
- [Pareto and findings](docs/PARETO_AND_FINDINGS.md)
- [Strategy Audit package](docs/STRATEGY_AUDIT_PACKAGE.md)
- [Query API and CLI](docs/QUERY_API_AND_CLI.md)
- [Backup/recovery/migrations](docs/BACKUP_RECOVERY_MIGRATION.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Performance](docs/PERFORMANCE_GUIDE.md)
- [Testing](docs/TESTING_GUIDE.md)
- [Operations](docs/OPERATIONS_RUNBOOK.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md)

## Comparison and Pareto trust boundary

Comparisons require exact immutable metric-definition content hashes on both
sides; matching names or version strings alone are insufficient. Pareto
directions come only from the Metric Definition Registry. Formula text is
stored as audit evidence and is never evaluated by this package.
