# System Acceptance — Trusted Production Phase Bindings V1

Owner Issue: #382  
Production binding Issue: #510  
Owner PR: #474

## Purpose

`applications/system_acceptance` is the durable acceptance state owner. It must not own a second Market Data, Backtest, Research or Trading Runner state machine, and it must never use GitHub Actions or browser state as runtime truth.

Production deployment supplies authoritative owner instances/frozen configuration through typed adapter boundaries. The browser continues to provide only the already-approved candidate fields; it cannot provide filesystem paths, commands, argv, credentials, output directories, Research repositories or runtime objects.

The complete registry is built with `TrustedProductionPhaseAdapters`. Missing or wrong phase types fail closed before the owner starts. Soak phases remain owned by `CheckpointedSoakExecutor`.

## Shared trusted identity

Every production adapter receives the same `TrustedAcceptanceIdentity`:

```text
full_rc_sha
dataset_id
dataset_digest
strategy_id
strategy_digest
config_digest
```

Before any external owner call, the adapter compares this trusted deployment identity with the persisted `PhaseExecutionContext`. A mismatch is `PHASE_BLOCKED`.

## ENVIRONMENT

`TrustedVerificationPhaseAdapter(ENVIRONMENT, ...)` accepts only a `VerificationProof` covering exactly these safe check codes:

```text
candidate
persistence
dataset_owner
backtest_owner
research_owner
runtime_owner
paper_only
```

The deployment-private verifier must actually evaluate those bindings. It may hash a richer private report, but System Acceptance receives only candidate digests, the safe check-code set, failed safe check codes and the aggregate SHA-256.

A missing required check is an invalid/recovery-required proof. A failed environment check blocks acceptance. A single synthetic `passed=true` flag is not a valid proof.

## DATASET

`TrustedDatasetPhaseAdapter` receives a zero-argument server-side resolver closure. The intended production closure wraps the accepted Market Data registered-dataset API such as `resolve_registered_kline_dataset(...)` with deployment-owned:

```text
storage_root
dataset version
timeframe
consumer/deep policy
```

The adapter never receives those values from browser JSON. The authoritative resolver already verifies registry identity/status, consumer compatibility, dataset integrity, manifest validity, partition continuity and bounds.

The adapter additionally requires the returned selection to match the persisted acceptance dataset identity and the deployment-owned version/timeframe. Recovery safely repeats this read-only integrity verification.

## BACKTEST_A / BACKTEST_B

`TrustedBacktestRunAdapter` receives a server-side callable for one frozen authoritative Backtest run. The callable receives the persisted `PhaseExecutionContext`, including the stable `attempt_id`, and returns `BacktestRunProof` only after authoritative result verification.

The production callable contract is:

1. use one frozen `BacktestAppConfig` and frozen module/strategy configuration;
2. use the registered dataset selected for this acceptance candidate;
3. use a deployment-owned output root; never a browser path;
4. execute the public Backtest application owner (`run_backtest` or an equivalent accepted owner boundary);
5. verify the published artifact manifest before creating the proof;
6. derive/verify the proof identities for the exact full-RC SHA, dataset digest, strategy digest and acceptance configuration digest;
7. compute the exposed artifact digest from verified authoritative publication metadata, never from an unverified path supplied by a client;
8. bind staging/publication recovery to the stable acceptance `attempt_id` where deployment state is needed.

Backtest publication is already fail-closed and idempotent for an existing deterministic destination: an identical verified manifest is accepted, while corruption/conflicting publication is rejected. Therefore recovery may repeat deterministic compute only through that same frozen binding; it must not switch config/output ownership or invent a new acceptance attempt.

Raw Backtest paths, manifests and exceptions are not projected by System Acceptance.

## DETERMINISM

`TrustedBacktestDeterminismAdapter` receives a deployment callable that compares the two independently verified A/B artifact identities and returns only `BacktestDeterminismProof`.

The phase succeeds only when the authoritative artifact SHA-256 identities are exactly equal. A mismatch is a factual `PHASE_FAILURE`; the threshold is not configurable by the browser.

## RESEARCH

`TrustedResearchPhaseAdapter` binds to an actual already-prepared durable `ResearchRunner` experiment through structural owner protocols. No direct dependency on the Research package is required by the clean System Acceptance wheel.

Deployment must provide the same frozen `PreparedExperiment` identity that was registered with the durable Research repository. The adapter verifies:

```text
PreparedExperiment.manifest.experiment_id
PreparedExperiment.specification.fingerprint
trusted acceptance candidate identity
```

It never calls `prepare()`.

This is important because Research provenance contains creation identity. Re-preparing after a crash with a different timestamp could create a provenance conflict even when the deterministic experiment ID is unchanged.

Execution/recovery rules:

- `COMPLETED`: succeed without another scheduler execution;
- `CREATED/VALIDATING/PLANNED/RUNNING/PAUSED`: call `ResearchRunner.execute(the_same_experiment_id)`;
- the accepted Research scheduler verifies existing artifacts, fences leases and resumes expired work for that same durable experiment;
- `BLOCKED`, `FAILED`, `COMPLETED_WITH_FAILURES`, `CORRUPTED`, `CANCELED`: fail/block/cancel without creating a second experiment;
- unsupported/invalid durable status: `RECOVERY_REQUIRED`.

Evidence is an opaque digest/reference derived from experiment identity + specification fingerprint + terminal status. Repository paths and raw errors are never projected.

## RUNTIME_SMOKE

System Acceptance must not call async Trading Runner lifecycle mutations from its synchronous daemon with `asyncio.run()` or another event loop.

`TrustedRuntimeSmokeAdapter` is read-only. Deployment builds `RuntimeSmokeProof` from the accepted Trading Runner owner health/config projection (for example `RuntimeHealthRegistry.snapshot()` plus trusted runtime/config identity). The proof must be:

```text
execution_mode = paper
ready = true
live = true
critical_failure_count = 0
full_rc_sha = acceptance candidate
config_digest = acceptance candidate
```

A not-ready/not-live/critical runtime is blocked, not synthesized as success. Runtime START/STOP/recovery remains owned by the accepted Trading Runner lifecycle owner.

## RECOVERY

`TrustedBacktestRecoveryAdapter` runs the frozen Backtest checkpoint/restart verification binding using the same stable System Acceptance attempt identity.

Success requires all of:

```text
restart_verified = true
continuous_sha256 present
restarted_sha256 present
continuous_sha256 == restarted_sha256
candidate identities match
```

Anything else is `PHASE_FAILURE`. The recovery proof is opaque; raw checkpoint/output paths are not evidence references.

## FINAL_VERIFICATION

`TrustedVerificationPhaseAdapter(FINAL_VERIFICATION, ...)` requires exactly these safe check codes:

```text
candidate
dataset
backtest_a
backtest_b
determinism
research
runtime_smoke
recovery
evidence
```

The deployment-private final verifier re-queries/re-validates the authoritative owner results for the same candidate and hashes its private report. System Acceptance receives only candidate digests, safe check codes/failures and the aggregate SHA-256.

Missing coverage is `RECOVERY_REQUIRED`; any failed final check is `PHASE_FAILURE`.

## Exception and privacy boundary

All production callbacks execute under `NativeOwnerPhaseExecutor`. Arbitrary owner exceptions become a bounded `OWNER_FAILURE` without chained raw exception text. Adapter proofs expose only bounded identifiers, booleans/counts and SHA-256 digests.

The following are never approved production adapter inputs from Control UI/browser:

```text
filesystem path
output directory
command / argv
shell script
GitHub workflow/run ID as runtime truth
API key / exchange credential
raw exception / traceback
Research DB path
Market Data storage root
Trading Runner object identity
```

## Deployment acceptance

Before #474/#382 may merge/close:

1. every typed adapter is backed by the real authoritative deployment owner/configuration, not a placeholder callback;
2. ENVIRONMENT and FINAL verification providers implement all mandatory safe check codes;
3. Backtest proof construction is tied to verified publication and frozen candidate config;
4. Research uses the already-registered frozen experiment identity and same durable scheduler;
5. runtime smoke is PAPER-only and read-only;
6. production composition builds the full non-soak registry plus owner-native soak executor;
7. restart/uncertain completion tests prove the same operation is recovered or safely idempotently reverified/recomputed, never switched to a new candidate;
8. privacy tests prove no path/secret/raw exception leakage;
9. exact-head Python 3.12/3.13 owner CI, Fast, policy and test-architecture checks pass after the final history-preserving full-RC sync.

Control Plane #371 remains a consumer only and must not be imported by these adapters.
