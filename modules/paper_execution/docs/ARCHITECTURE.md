# Architecture

`PaperExecutionCoordinator` is a paper-only orchestration layer over Core. It depends on `CorePaperVenue`, a structural protocol matching Core submit, cancel, pending-order, event-processing and position-protection operations. No broker, event bus, portfolio store, strategy, risk engine or accounting source of truth is reimplemented.

Core remains authoritative for orders, fills and positions. Paper Execution maintains only deterministic derived projections needed for idempotency, orchestration, recovery and reconciliation.

## Canonical input flow

`DownstreamPlanEnvelope -> Router hash/validity/OCC validation -> canonical Paper plan adapter -> market and factual-position validation -> metadata snapshot -> Core ExchangeRulesAdapter -> Core paper venue`

The Router adapter preserves canonical JSON, hashes, validity and ordered string OCC evidence. Local integer `StateVersions` remain a separate Paper precondition and never replace or reinterpret Router OCC. Open, add, reduce and close are supported. Reversal is typed-rejected.

## Durable execution boundary

The base coordinator enforces:

`preflight -> execution.intent_recorded -> Core cancellation/submission side effects -> pre-acceptance orchestration hooks -> plan.accepted`

Pre-acceptance hooks register submitted OCO groups before durable acceptance. Failure before acceptance enters the existing compensation path. A failed acceptance commit compensates submitted orders and blocks the execution scope.

An existing journal containing execution intent evidence does not, by itself, reconstruct coordinator memory. Restart requires an attested checkpoint and mandatory order/position reconciliation before further execution.

## Market compatibility and precision

The plan market is checked against venue metadata before any order side effect. Compatibility uses an explicit allowlist for canonical identifiers and known aliases. Unknown identifiers and cross-market combinations fail closed.

Core is the exchange-rules authority. Decimal inputs cross the Core float API only after lossless round-trip checks. Core-normalized values are converted back to Decimal and revalidated against original tick, step, min/max and notional constraints. Overflow, underflow-to-zero and precision loss fail closed.

## Fill-driven protection

Deferred protection activates only after factual Core fill state is projected:

`Core OrderEvent -> ProtectionCoordinator -> fresh VenueMarkSnapshot -> Core update_position -> committed activation -> journal evidence`

`VenueMarkSnapshot` carries instrument, positive price, UTC observation time and source. The deterministic paper contract requires an exact instrument match and `observed_at` equal to the fill/retry event timestamp. There is no fallback to average entry price.

For add-to-position, the Router execution wrapper requires a factual Core position snapshot. The protection coordinator stores the verified baseline quantity and protects `baseline + cumulative add fill`. Baseline state is persisted in recovery schema v4.

## Synthetic OCO orchestration

OCO groups are built from actually submitted Core order projections and registered inside the execution transaction before `plan.accepted`.

`Core OrderEvent -> local cumulative OCO fill -> required sibling adjustment -> Core cancel -> confirmed local cancellation`

Cancellation is committed locally only when Core reports a positive match. Duplicate events return without repeating side effects. Cancel failure, missing sibling state and late fill/cancel races block the scope and require reconciliation. Since Core exposes no amend path, a partial fill cancels the unsafe residual sibling and blocks instead of emulating unsupported resize semantics.

## Reconciliation ownership

Order and position reconciliation use portfolio/instrument scopes and Core snapshots. Safe orphan-projection repair is allowed only for one unique non-terminal order owned by this component. Ownership of portfolio, instrument and intent role is proven from the canonical snapshot identity tag using digest verification; caller-provided metadata is not sufficient.

## Recovery flow

`Core EventStore + schema-v4 ModuleState -> hash/schema validation -> temporary reconstruction -> journal/timestamp attestation -> atomic restore -> mandatory order and position reconciliation -> ready`

Schema v4 persists plan results/scopes, order projections and event fingerprints, last event timestamps, blocked/recovery scopes, OCO and deferred-protection orchestration, and exact journal attestation. Schema v1-v3 checkpoints migrate forward.

Journal and per-order event timestamps are non-decreasing. A new sequence with an older timestamp is rejected without mutation, including after restore. Corruption, unknown fields, duplicate IDs, sequence gaps, configuration mismatch, event-store mismatch or failed invariants leave the prior coordinator state unchanged.

## Integration gates

The permanent `paper-execution-ci` workflow validates Python 3.12 and 3.13 with the exact Router contract, compileall, Ruff, strict MyPy, paper-only security scans, full branch-aware coverage, reproducible wheel/sdist builds and clean-wheel smoke.

The all-modules `integration-full-ci` additionally validates Core and neighbouring modules. Both coverage gates use Bash `pipefail`, two-decimal precision and a hard minimum of 85%.
