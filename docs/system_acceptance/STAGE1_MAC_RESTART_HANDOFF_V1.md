# Stage-1 Mac restart and launchd handoff V1

## Scope

This contract closes the repository-side restart boundary for the Mac-local Stage-1
owner graph. It does not start `SOAK_2H`, authorize 24h/72h, enable LIVE execution, or
turn GitHub evidence into runtime identity.

The trusted deployment owns all paths, expected identities, tokens, and concrete owner
component factories. No browser or CLI input selects Python modules, classes, commands,
artifact IDs, experiment IDs, runtime IDs, or secrets.

## Initial Trading provisioning

If the authoritative Trading durable owner state does not exist yet, the trusted
deployment must not hand-write the identity manifest, runtime state, checkpoint, or
journal. It constructs `TradingRunnerProvisioningFactory` with one fixed PAPER
configuration, a new fixed owner root, the final identity path, a fixed run identity,
and the same deployment-owned typed `TradingRunnerComponentsFactory` contract used for
restart.

The provisioner creates state only through the real Trading owner lifecycle. It starts
and stops one real `TradingRunner`, requires the canonical
`RuntimeCheckpointCoordinator` and `RuntimeRecoveryCoordinator` to be bound to the same
owner stores and journal, verifies the resulting stopped runtime/checkpoint/hash-chain
state, then reconstructs that owner through `TradingRunnerBootstrapFactory` and
exercises one real recovered start/stop. The strict durable identity manifest is
published only after that restart proof succeeds. Existing or partial owner roots,
non-PAPER configuration, component/store identity drift, failed lifecycle/recovery, or
identity publication failure all fail closed.

`TradingRunnerProvisioningFactory` does not choose concrete modules/adapters or create a
second runtime state machine. The deployment still owns the fixed code objects and
private configuration. After one successful provision, normal service startup uses
`TradingRunnerBootstrapFactory`; provisioning is not repeated on an existing owner.

## Typed reconstruction

`ApprovedStrategyPackageLoader` reads exactly one configured
`strategy_package-<sha256>` from `ContentAddressedArtifactStore`, checks the strict
stage-artifact schema, reconstructs `StrategyPackage` with `decode_strategy_package()`,
runs `StrategyPackageBuilder.verify()`, requires `APPROVED_FOR_PAPER`, and rejects all
identity drift or LIVE authorization.

`PreparedExperimentLoader` is constructed around one code-owned `ResearchRunner` and
one configured experiment identity. It reads the authoritative SQLite experiment row
and the fixed `experiments/<id>/{specification,manifest,plan}.json` artifacts. It
reconstructs a typed `ResearchSpecification`, deterministic `ExperimentPlan`, and
typed `ExperimentManifest`; compares canonical artifacts, repository manifest digest,
strategy ID, specification fingerprint, and derived experiment ID; and returns the
original `PreparedExperiment`. It never calls `prepare()` or registers a new experiment.

`TradingRunnerBootstrapFactory` reads one fixed PAPER YAML configuration, one fixed
durable identity manifest, the existing Core runtime/checkpoint state, and the existing
hash-chain journal. Checkpoint and journal run identities must match. A deployment-owned
typed `TradingRunnerComponentsFactory` supplies the concrete
code objects; persisted data never names/imports classes. The factory creates exactly
one `TradingRunner` and wraps that same object in one
`TradingRunnerOperatorLifecycle`. Missing/corrupt state or runtime, portfolio, strategy,
configuration, execution-mode, or journal identity drift fails before owner start.

## Service lifecycle

`Stage1MacServiceManager` receives only typed owner/source factories constructed by the
trusted deployment entrypoint. In one process it:

1. reconstructs the three authoritative owners;
2. starts the reconstructed Trading Runner and its loopback lifecycle transport on
   `127.0.0.1:8780`;
3. exposes the already-prepared Research owner on `127.0.0.1:8782`;
4. builds strict HTTP clients bound to those exact owners;
5. constructs `Stage1DeploymentSources` through the deployment-owned typed source
   factory and calls `materialize_stage1_deployment()`;
6. constructs `Stage1MacServiceHost` (which forms `Stage1PrestartProof` before start),
   atomically persists only the already-formed `Stage1PrestartProof.to_json()`, and only
   after that proof succeeds starts System Acceptance on `127.0.0.1:8781`;
7. closes System Acceptance, Research transport, Trading transport, and the Trading
   owner in reverse order on `SIGTERM`/`SIGINT`.

Tokens are private dataclass fields and never appear in proof, plist, argv, or repr.
The proof keeps `allow_soak_24h=false` and `allow_soak_72h=false`.

## User LaunchAgent

`Stage1LaunchAgentConfig` and `install_stage1_launch_agent()` render/install the fixed
user label `com.mishkastrategy.module-strategy.stage1`. The plist has one executable
path and no shell, arbitrary arguments, environment, or credentials. It uses the
dedicated checkout as `WorkingDirectory`, `RunAtLoad=true`, and
`KeepAlive.SuccessfulExit=false`. Installation is atomic and mode `0600`.

The executable is a deployment-owned, no-argument wrapper that constructs the fixed
typed loaders, component/source factories, private token configuration, and calls
`Stage1MacServiceManager.run()`. The repository intentionally does not invent that
machine's missing Strategy/Research/Trading identities or concrete execution adapters.
`launchctl bootstrap` remains an explicit local operator action after private state and
the wrapper have been provisioned and verified.

## Physical acceptance boundary

CI proves reconstruction, corruption/mismatch failure, one-owner binding, loopback
lifecycle, sanitized proof, restart/reconnect continuity, disabled 24h/72h policy, and
secret-safe launchd material. It does not prove that a particular Mac has the required
private durable state. Issue #873 remains unaccepted until that Mac provisions the
authoritative state and wrapper, installs/bootstraps the LaunchAgent, and captures the
sanitized disconnect/restart/reconnect proof. Issue #866 `SOAK_2H` remains blocked until
then.
