# System Acceptance Stage-1 deployment bindings V1

## Purpose

This document defines the reproducible deployment/service-manager composition for the
baseline two-hour PAPER System Acceptance stage.

The executable repository-owned composition root is:

```text
scripts/system_acceptance_stage1_deployment.py
```

Its cross-owner integration gate is:

```text
tests/integration/test_system_acceptance_stage1_deployment.py
.github/workflows/system-acceptance-stage1-composition-ci.yml
```

The composition root deliberately lives outside `applications/system_acceptance` so the
System Acceptance owner package remains dependency-free. The deployment environment
installs the authoritative Market Data, Backtest, Research, Strategy Factory and Trading
Runner packages and passes their already-owned objects into the composition root.

This design does **not** add a generic CLI and does not move runtime ownership into the
browser, GitHub Actions, or shell sleeps. The durable `SQLiteAcceptanceStore` and the
accepted System Acceptance owner remain authoritative runtime truth.

Stage 1 is PAPER-only and always uses `allow_soak_24h=False`. A later 24-hour acceptance
is a fresh run under separately authorized deployment policy; it is not an extension or
mutation of the Stage-1 request.

## Trusted deployment boundary

Only the local deployment/service manager may resolve or hold:

- the pinned full-RC SHA;
- Market Data storage roots and registry files;
- Backtest module/output paths;
- Research repository/state objects;
- the verified Strategy Factory `StrategyPackage`;
- the live in-process PAPER `TradingRunner` object;
- System Acceptance SQLite state path;
- reader/operator bearer tokens;
- private credentials or filesystem layout.

Those values must never be supplied by the browser or serialized into the public
`AcceptanceConfigurationMaterial`. Private paths and secrets may affect local owner
construction, but public/sanitized evidence contains only safe stable identities and
SHA-256 fingerprints.

`Stage1DeploymentSources` marks filesystem/runtime/callback fields `repr=False`, while
`Stage1DeploymentComposition.material` is the safe, deterministic semantic identity.

## Canonical owner contract identity

Stage 1 uses exactly `STAGE1_OWNER_CONTRACTS_V1`. The accepted owner/version pairs are
part of the `config_digest` and cannot be replaced by deployment-chosen labels:

- Backtest: `application-v1`;
- Market Data: `registered-dataset-v1`;
- Research: `prepared-experiment-v1`;
- Strategy Factory: `strategy-package-v1`;
- System Acceptance: `production-bootstrap-v1`;
- Trading Runner: `paper-runtime-v1`.

Changing this integration contract requires a new explicit version rather than silently
reusing V1.

## 1. Resolve registered Market Data

The composition uses the existing authoritative API:

```python
from market_data_ingestion import resolve_registered_kline_dataset

selection = resolve_registered_kline_dataset(
    storage_root,
    dataset_id,
    dataset_version,
    timeframe="5m",
    consumer="modular_backtest",
    deep=True,
)
```

The resolver validates the registry, descriptor identity, VALID state, compatible
consumer, deep storage integrity, manifest, partitions, continuity and bounds.

Stage-1 identity uses exactly:

```text
dataset_id        = selection.descriptor.dataset_id
dataset_digest    = selection.descriptor.dataset_hash
dataset_version   = selection.descriptor.version
dataset_timeframe = selection.timeframe
```

Do not substitute raw parquet SHA-256, `download_summary.csv` SHA-256 or an external
archive checksum for `descriptor.dataset_hash`.

The `dataset_resolver` is a zero-argument server-side closure over the same trusted
storage root/id/version/timeframe. It resolves and deep-validates the registered dataset
again during Stage-1 assembly/recovery instead of trusting stale browser state.

## 2. Verify the authoritative StrategyPackage

The composition uses:

```python
from strategy_factory.package import StrategyPackageBuilder

StrategyPackageBuilder().verify(strategy_package)
```

It additionally requires:

- `admission_status == APPROVED_FOR_PAPER`;
- `live_trading_authorized == False`;
- the prepared Research `strategy_id` to equal the package `strategy_id`;
- the Trading Runner `strategy_id` to equal the same package `strategy_id`.

Stage-1 identity uses only:

```text
strategy_id = strategy_package.strategy_id
strategy_package_hash = strategy_package.package_hash
```

Do not substitute candidate id, composition hash, semantic digest, wheel hash or CI
evidence hash.

`bind_stage1_production_sources()` re-runs the supplied package verifier before the owner
process is constructed and requires the verified id/hash to equal the frozen Stage-1
material.

## 3. Freeze Backtest A, B and recovery execution identity

The deployment constructs three authoritative `BacktestAppConfig` objects before START:

- Backtest A with checkpoint mode disabled;
- Backtest B with checkpoint mode disabled;
- recovery Backtest with replay/restart semantics.

All three must use distinct private output roots. A and B remain semantically identical;
recovery may differ only by checkpoint/restart semantics. Output roots do not affect the
semantic public digest.

The composition rebuilds the execution identity from the same objects that the real
callbacks execute:

```python
backtest_identity = build_backtest_execution_identity(
    backtest_a_config=backtest_a_config,
    backtest_b_config=backtest_b_config,
    recovery_config=recovery_config,
    performance=performance_config,
    dataset_id=selection.descriptor.dataset_id,
    dataset_digest=selection.descriptor.dataset_hash,
    dataset_version=selection.descriptor.version,
    dataset_timeframe=selection.timeframe,
    valuation_policy=valuation_policy,
    funding_source=funding_source,
    mark_source=mark_source,
)
```

The identity includes the accepted Backtest dataclass field sets, semantic data
configuration, module configuration **content** SHA-256, execution/fees/risk/instrument,
seed, checkpoint semantics, end-of-test policy, every `PerformanceConfig` field,
effective registered-data mode/batch overrides, and authoritative funding/mark source
identities.

Raw storage/output/module paths are not serialized. A new Backtest dataclass field fails
closed until the identity contract is deliberately updated.

The environment overrides below are part of the effective identity and any drift is
caught by the execution-identity probe before A/B/recovery callbacks:

```text
MODULAR_BACKTEST_REGISTERED_DATASET_MODE
MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE
```

## 4. Build Backtest proofs from verified durable artifacts

Do not construct `BacktestRunProof` manually.

The repository composition wraps the real `modular_backtest.run_backtest()` callbacks
with `VerifiedBacktestRunBinding` and the official
`modular_backtest.results.verify_artifact_manifest` verifier.

For recovery it uses `require_restart=True`. The binding verifies:

- `artifact-manifest.json`;
- `application-run.json`;
- run/application ids against `RunOutcome`;
- restart status;
- continuous/restarted SHA-256 equivalence.

For determinism it uses `VerifiedBacktestDeterminismBinding` over the two actual A/B
result roots. The roots must be distinct and both manifests are re-verified on every
comparison, including after System Acceptance owner restart.

Private result paths never become evidence.

## 5. Bind an already-prepared Research experiment

System Acceptance must not create a Research experiment.

The deployment prepares the experiment through the authoritative `ResearchRunner`
before composition and passes both the real runner and `PreparedExperiment` into
`Stage1DeploymentSources`.

The frozen identity is:

```text
research_experiment_id              = prepared.manifest.experiment_id
research_specification_fingerprint  = prepared.specification.fingerprint
```

Both values are required. `ResearchRunner.prepare()` derives `experiment_id` from the
specification together with the Backtest Engine, metric-schema and module-API versions,
so the specification fingerprint alone is not sufficient to identify one concrete
prepared execution contract.

`materialize_stage1_deployment()` never calls `ResearchRunner.prepare()`. The existing
`TrustedResearchPhaseAdapter` resumes/reads the same durable experiment. Before START,
`bind_stage1_production_sources()` requires both the prepared experiment id and the
specification fingerprint to equal the frozen material.

The integration test prepares the real experiment first and then replaces `prepare()`
with a failing sentinel before composition, proving Stage-1 assembly cannot create a new
experiment accidentally.

## 6. Bind the PAPER Trading Runner read-only owner state

Stage-1 attaches to the already-owned `TradingRunner`; it does not create, start, stop or
mutate a second runtime.

The full-RC integration parent does not yet contain the later Control UI
`ReadOnlyTradingRuntimeQuery` helper. Therefore this V1 composition uses the public
full-RC owner objects that are already available on the accepted parent:

```text
TradingRunner.config.canonical_hash
TradingRunner.config.runtime_id
TradingRunner.config.portfolio_id
TradingRunner.config.strategy_id
TradingRunner.config.execution_mode
RuntimeHealthRegistry.snapshot()
```

These are projected through typed `RuntimeConfigurationIdentity`. The composition
requires `runner.health` to be the authoritative `RuntimeHealthRegistry`, not an
arbitrary health double, and derives the sanitized runtime proof from that registry only.

Before assembly and before every runtime-smoke callback it rebuilds the complete runtime
identity and requires exact equality with the frozen material:

```text
trading_runner_config_digest
authoritative runtime_id
authoritative portfolio_id
strategy_id
execution_mode == paper
```

It also requires Trading Runner `base_timeframe` to equal the Stage-1 registered dataset
timeframe. `RuntimeSmokeProof.runtime_id` is checked again against the frozen runtime id,
so a health projection from a different runtime instance fails closed.

`RuntimeSmokeProof.config_digest` is the overall
`AcceptanceConfigurationMaterial.config_digest` required by the System Acceptance
runtime adapter. `material.trading_runner_config_digest` remains the component-level
canonical hash used inside the runtime identity.

The same health/config identity probe drives `CheckpointedSoakExecutor`; unhealthy,
non-live or drifted runtime state contributes zero healthy soak time.

## 7. Build AcceptanceConfigurationMaterial

After all owner identities are resolved, the composition constructs exactly one:

```python
material = AcceptanceConfigurationMaterial(
    full_rc_sha=pinned_full_rc_sha,
    dataset_id=selection.descriptor.dataset_id,
    dataset_digest=selection.descriptor.dataset_hash,
    dataset_version=selection.descriptor.version,
    dataset_timeframe=selection.timeframe,
    strategy_id=strategy_package.strategy_id,
    strategy_package_hash=strategy_package.package_hash,
    backtest_config_digest=backtest_identity.config_digest,
    backtest_performance_digest=backtest_identity.performance_digest,
    valuation_policy=backtest_identity.valuation_policy,
    funding_identity_digest=backtest_identity.funding_identity_digest,
    mark_identity_digest=backtest_identity.mark_identity_digest,
    trading_runner_config_digest=runtime_identity.config_digest,
    trading_runner_runtime_id=runtime_identity.runtime_id,
    trading_runner_portfolio_id=runtime_identity.portfolio_id,
    research_experiment_id=prepared.manifest.experiment_id,
    research_specification_fingerprint=prepared.specification.fingerprint,
    owner_contracts=STAGE1_OWNER_CONTRACTS_V1,
)
```

The canonical `trading_runner` object additionally contains the globally frozen
`strategy_id` and explicit `execution_mode = paper`. The canonical `research` object
contains both experiment id and specification fingerprint. Sorted compact UTF-8 JSON
with non-finite values rejected is hashed with SHA-256.

`material.config_digest` is the only Stage-1 deployment config digest. Do not replace it
with Core, Strategy Factory, Trading Runner or Backtest component hashes.

`material.trusted_identity()` produces the durable candidate identity used by every
System Acceptance production phase.

## 8. Materialize bindings and build the process

The deployment/service manager supplies fixed owner objects to:

```python
from scripts.system_acceptance_stage1_deployment import (
    Stage1DeploymentSources,
    materialize_stage1_deployment,
)

composition = materialize_stage1_deployment(
    Stage1DeploymentSources(
        full_rc_sha=pinned_full_rc_sha,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_timeframe="5m",
        dataset_storage_root=private_market_data_root,
        strategy_package=strategy_package,
        backtest_a_config=backtest_a_config,
        backtest_b_config=backtest_b_config,
        recovery_config=recovery_config,
        performance=performance_config,
        research_runner=research_runner,
        research_prepared=prepared,
        trading_runner=trading_runner,
        environment_verify=environment_verify,
        final_verify=final_verify,
    )
)
```

`materialize_stage1_deployment()` supplies all fields of
`SystemAcceptanceProductionBindings` using the real owner APIs/factories. Test doubles
are needed only at external/runtime boundaries when the integration gate constructs a
real Trading Runner or Research scheduler fixture.

Private process state and tokens remain outside the public material:

```python
process = composition.build_process(
    state_path=private_acceptance_state_path,
    reader_token=private_reader_token,
    operator_token=private_operator_token,
)
```

The process builder revalidates StrategyPackage, registered dataset, Backtest execution
identity, the concrete prepared Research experiment/fingerprint and the complete typed
Trading Runner identity before the owner can START. Stage 1 hard-codes
`allow_soak_24h=False`.

## 9. Exact-head composition CI

`.github/workflows/system-acceptance-stage1-composition-ci.yml` runs on
`[self-hosted, fast]` for Python 3.12 and 3.13. It:

1. checks out and verifies the exact PR head SHA;
2. runs the standard `fast` runner preflight;
3. installs the local authoritative owner packages explicitly;
4. runs `uv pip check`;
5. compiles the composition and integration test;
6. runs Ruff lint and format checks;
7. runs strict MyPy on the composition root;
8. runs the focused real cross-owner integration tests.

The gate does not upload routine successful GitHub Actions artifacts. Any diagnostic
artifact policy remains governed by the organization CI storage policy.

The general Core CI intentionally excludes only
`tests/integration/test_system_acceptance_stage1_deployment.py` from its lightweight
integration invocation because Core CI does not install the full Market Data/Research/
System Acceptance owner dependency surface. The test remains mapped by Test Architecture
Validation and is mandatory in this dedicated Python 3.12/3.13 composition gate.

## 10. START request

For baseline Stage 1 the Control Plane START request must match server-side material:

```text
dataset_id       = material.dataset_id
dataset_digest   = material.dataset_digest
strategy_id      = material.strategy_id
strategy_digest  = material.strategy_package_hash
config_digest    = material.config_digest
enable_soak_24h  = false
enable_soak_72h  = false
```

The full-RC SHA is pinned server-side and is not supplied by the browser request. Any
mismatch fails closed.

## 11. Restart behavior

The service manager reconstructs the same frozen deployment objects after process
restart:

1. resolve the same registered dataset identity;
2. load and verify the same approved PAPER StrategyPackage;
3. reconstruct the same A/B/recovery Backtest semantic configs and PerformanceConfig;
4. resolve the same funding/mark identities when enabled;
5. load the same prepared Research experiment id and specification fingerprint;
6. attach to the same PAPER Trading Runner config hash/runtime id/portfolio id/strategy id and health registry;
7. compute the same `AcceptanceConfigurationMaterial.config_digest`;
8. open the same private `SQLiteAcceptanceStore` state path;
9. start the pinned loopback owner transport with distinct reader/operator tokens.

The concrete typed repository boundaries for these steps are documented in
`STAGE1_MAC_RESTART_HANDOFF_V1.md`. They reconstruct the strict StrategyPackage and
PreparedExperiment types, create one authoritative Trading Runner through a code-owned
components factory, and coordinate the three fixed loopback transports under one
signal-aware service-manager lifetime. Persisted state is validation input only and
never selects Python imports or commands.

If any semantic identity changes, reconstruction fails closed rather than silently
resuming an old acceptance run under new semantics.

## 12. Stage boundary

A successful two-hour Stage-1 run does not authorize 24 hours automatically.

Stage 2 requires a new run/request identity and separately authorized deployment policy.
It must rerun the validation graph and its own two-hour phase before entering 24 hours.
72-hour soak remains outside this V1 acceptance plan.
