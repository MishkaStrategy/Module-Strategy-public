# Strategy Lifecycle ADR for #395

**Status:** Draft for owner review; not accepted and not implementation authority.  
**Scope:** PAPER-only strategy governance after immutable candidate/package/audit evidence exists.  
**Issue:** [#395 — accepted strategy runtime lifecycle](https://github.com/MishkaStrategy/Module-Strategy/issues/395)

## Context

The product has several valid but separate owner contracts. Strategy Factory represents research runs, immutable `StrategyPackage` artifacts, audit decisions, and PAPER admission facts. Trading Runner owns runtime operations. Risk Supervisor owns current safety verdicts and HALT/recovery. Control Plane currently projects Factory strategy runs. No accepted or implemented component yet owns the post-package strategy lifecycle requested by #395.

The target labels in #395 are `Candidate → Audited → PaperAccepted → Running → Suspended → Superseded → Archived`. These labels must not become a second inferred runtime state machine. The owner selected a separate application-level Strategy Lifecycle owner alongside Strategy Factory; its durable per-instance state and common fence are checked by Trading Runner before every effect. The owner selected a Core-owned SQLite ledger for local implementation; service identity, database path, deployment and retention remain unresolved. The owner also selected per-instance generation checks in Core for PAPER effects; this supersedes the earlier draft statement that Core remains unchanged. LIVE trading is out of scope.

**Owner-selected Core generation enforcement (2026-10-25; implementation contract still draft):** every mutating Core boundary must compare the current per-instance generation and commit the effect under the same serialized durable Core commit domain. A generation read before an ordinary Core call is not atomic and leaves a TOCTOU race. Lifecycle records a transition request durably and reports SUSPEND as enforced only after a durable Core fence receipt; without that receipt, status remains pending/unavailable. Core rejects new-risk effects after the fence commit. Factual fills that arrive after SUSPEND must still be validated, deduplicated, attributed to their original instance, and ingested. Protective/reduce-only continuation requires trusted classification; the exact authority for weakening/replacing protection is unresolved and defaults to deny. The current Core has no durable effect-commit owner and no per-instance order/position attribution. These requirements are architecture-derived refinements, not implemented production guarantees or PAPER authorization.

## Candidate local generation/CAS slice (2026-10-25T16:06:00+07:00; uncommitted)

The separate `release-manifest-387-v2` candidate adds Lifecycle intent generation and expected-generation CAS, idempotent replay, explicit `SUSPEND_PENDING/PENDING_CORE_COMMIT`, and Core-owned immutable effect/fence request and receipt contracts. Strategy Lifecycle SQLite schema v1→v3 migration normalizes legacy SUSPENDED without claiming Core enforcement, installs a stale-writer generation trigger, and serializes startup version/shape checks and migration. Focused lifecycle tests passed 11; Ruff/format/compile/diff/Graphify checks passed. At that checkpoint the selected Core-owned SQLite execution ledger was not implemented; see the subsequent local implementation slice below.

This initial generation/CAS evidence was candidate local owner storage and protocol shape only. Revision-760 below records the later Core SQLite reference slice; it does not provide production effect/replay/recovery wiring, per-instance Paper order/position ownership, or production readiness authority. RESUME, supersession and effect dispatch remain unavailable. #395 remains OPEN/unaccepted; this evidence does not close the acceptance sequence or authorize PAPER.


## Revision-760 Core-owned SQLite reference slice (2026-10-25; local-only)

Following the owner's selection, `src/modular_core/lifecycle_sqlite.py` adds an opt-in `SQLiteCoreLifecycleCommitPort` for one immutable instance/broker aggregate. It uses `BEGIN IMMEDIATE`, checks the stored binding, writer epoch, generation and pinned authority-reference inventory, stages supported broker commands on a private restored `BrokerSimulator`, and commits broker snapshot, event delta/commit marker, idempotency receipt and lifecycle fence together. Core SUSPEND atomically advances generation and closes new risk. Effect markers preserve quiet-bar progress and bind each command receipt to a contiguous event range; replay and reopen reject missing or mismatched markers. Supported local commands include submit/cancel, protection update, scheduled close, bar processing and cancel-all. The risk fence allows verified risk-reducing execution, including partial close and reduce-only fills, while rejecting removal/weakening of active protection. RESUME is rejected.

The focused Core SQLite, Strategy Lifecycle and broker-state selection passed 38 tests. Targeted Ruff check/format, `py_compile`, and Graphify update passed; GPT-6.1 Sol's narrow review found no remaining P0–P2 in event-time and partial-exit handling. These checks establish only this local reference's tested behavior. It is not wired into Paper Execution, Runner, Control Plane or recovery. Authority refs are supplied by the host and are not authenticated/currentness-checked here; there is no identity issuer, writer handoff, persistent production path/retention policy, migration of active Paper state, or end-to-end effect-path coverage. Lifecycle still cannot report production SUSPEND as enforced; keep #395 OPEN/unaccepted and PAPER, CI, deployment and release gates closed.

## Owner-selected ownership boundary (contract still draft)

On 2026-10-24 at approximately 00:23 Novosibirsk (2026-10-23T17:23Z), the owner selected a separate Strategy Lifecycle owner for durable per-instance state, with Trading Runner checking the shared fence before every effect. The owner additionally selected (2026-10-25) that a package may serve multiple independent instances and one Runner/runtime may serve multiple instances; same-instance recovery preserves owner-issued identity/incarnation, clones receive new identities, supersession links records, and replacement waits until open orders and positions reconcile flat. This does not identify a deployed service, accountable operator, trusted command boundary, store, deployment, identity issuer, or evidence sources. #395 remains OPEN, this ADR is not accepted, and PAPER remains unauthorized.

**Direct owner confirmation — 2026-10-24T02:25:24+07:00 / 2026-10-23T19:25:24Z:** the owner reconfirmed the same boundary in this chat: a separate Strategy Lifecycle owner stores durable per-instance state, and Runner checks the common risk fence before every effect. No contract or release gate was closed by this confirmation.

“Every effect” must be made concrete across plan admission and continuation, order submission/cancellation, close and protection updates, fills and recovery/replay paths. The fence must classify the effect against the owner-selected SUSPEND rules: block new risk and preserve verified protective/reduce-only effects. Draft interpretation for cancellation, pending later owner-contract validation: after durable SUSPEND, an identity-scoped cancellation request may be sent; keep pending state and reservation until an authoritative per-order terminal/reconciled outcome, because request ACK or timeout is not completion. The current global Runner kill switch is not the common per-instance fence.

The read-only effect-path inventory and typed fence proposal are tracked separately in [the #395 fence/effect-inventory draft](STRATEGY_LIFECYCLE_FENCE_EFFECT_INVENTORY_DRAFT.md).

## Scope and non-goals

The lifecycle owner records which immutable strategy identity is governed, the evidence and policy that permit each governance transition, and whether admission or continuation is currently permitted. It does not calculate strategy signals, edit package contents, replace Runner execution state, replace Risk Supervisor verdicts, or make Control Plane authoritative.

The owner must fail closed on unknown, stale, revoked, conflicting, or unverifiable evidence. Restart must not grant permission, clear a suspension, or infer that an uncertain side effect did not occur.

## Authorities and ownership

| Concern | Authoritative source | Lifecycle owner behavior |
|---|---|---|
| Candidate, package contents, audit result, PAPER acceptance | Strategy Factory / Auditor and the immutable package/audit artifacts | Verify and reference exact identities; do not rewrite or infer these facts |
| Runtime start, stop, kill switch, reset, and operator replay | Trading Runner's owner-native lifecycle | Request or observe typed operations; report Runner state separately |
| Current system safety verdict and HALT/recovery | Risk Supervisor | Consume typed verdict/evidence under an explicitly accepted policy; do not mutate Supervisor or Runner state |
| Strategy governance state and transition history | **Owner-selected target:** separate application-level Strategy Lifecycle owner | Own one durable, versioned transition log and per-instance fence/decision; exact service and store are not yet identified |
| Operator/read projection | Control Plane | Read and render typed owner projections; do not accept lifecycle mutations or maintain another lifecycle store |

The Control Plane Strategy view currently reflects Factory run checkpoints/status, not accepted-strategy lifecycle. It must not label a Factory run `Running` or `Suspended` as a strategy governance fact.

## Immutable identity and evidence binding

Every lifecycle record and transition must identify the exact candidate, `strategy_id`, package artifact/hash, audit decision/hash, and any accepted policy/evidence revisions. The owner must reject a transition if evidence refers to another identity, has been revoked, has expired under an accepted policy, or cannot be independently verified.

Every command must include:

- a stable command/request ID and canonical request/payload hash;
- the immutable candidate/package/audit identity tuple;
- expected lifecycle revision;
- trusted actor identity from an authenticated owner boundary;
- policy version and typed owner-evidence references.

Raw browser bearer values, free-form reasons, untrusted source-commit claims, and caller-selected evidence attachments are not authority. Evidence references must resolve through their owning stores.

## State model proposal

The seven #395 labels describe **governance state**. Runtime execution state and Risk Supervisor verdict are separate typed projections. The product must decide the meaning of `Running` before implementation:

Owner-selected `Running` meaning: the exact immutable package is authorized and bound to the instance/runtime. Observed Runner status is a separate fact.
Alternative not selected: define `Running` as confirmed execution and represent authorization/binding separately.

The owner selected `Running` to mean the instance is authorized and bound. Observed Runner status remains separate and must not be conflated with governance state. The draft recommends exposing both `governance_state` and `runner_state`, with explicit binding identity.

Likewise, governance `Suspended`, Risk Supervisor `HALT`, and Runner `STOPPED` are different facts. The owner has selected the SUSPEND/RESUME behavior recorded below, but has not accepted the lifecycle state model or the source authorities that qualify it. A Supervisor HALT must not silently rewrite governance state; a governance suspension must not claim that Runner stopped. Do not implement strategy suspension by mapping it to Runner `PAUSED`, Kill Switch, or STOP: those are distinct runtime controls with different scope and effects.

## Owner-confirmed SUSPEND/RESUME behavior (policy selected; contract unaccepted)

The owner selected these strategy-instance behavior rules:

- SUSPEND blocks new risk for that strategy instance.
- SUSPEND does not automatically close an open position.
- Pending or unconfirmed entries are cancelled only when the cancellation outcome is authoritative.
- Verified protective and reduce-only actions continue while suspended.
- The system never resumes automatically, including after restart or recovery.
- RESUME requires an explicit operator decision and fresh validation of plan, data, and protection.

The owner reconfirmed these rules on 2026-10-23 (22:30 Novosibirsk / 2026-10-23T15:30:30Z); the behavior policy is unchanged from revision 622.

This confirms behavior policy only. The owner additionally selected `Running` as authorized/bound with observed Runner status separate. RESUME uses direct canonical-owner reads plus commit-time currentness checks; missing sources fail closed. Ordinary RESUME reuses audit/PAPER admission only while current and unrevoked, with fresh plan/data/protection checks; repeat admission is required after revoke/expiry or for a new package. Concrete source/verifier identities, trusted operator/auth authority, binding authority and re-binding sequence when current binding is absent, and implementation remain open. RESUME governance target is `Running`. #395 remains OPEN and unaccepted; no PAPER authority is granted.

## Sol architecture review: required proposal invariants (not owner acceptance)

The completed architecture review recommends two explicit, orthogonal fields for the strategy-instance risk gate and qualification:

- `risk_gate`: `ENABLED` or `SUSPENDED`;
- `qualification`: `VERIFIED`, `CANCEL_PENDING`, or `RECOVERY_REQUIRED`.

These proposal fields do not replace the #395 governance labels, Runner state, or Supervisor verdict. The owner selected Strategy Lifecycle as the separate authority for durable per-instance state; the exact durable API, evidence sources, and transition mapping remain unresolved. If adopted, the implementation and acceptance evidence must preserve these invariants:

1. Commit the SUSPEND risk gate and a durable fence atomically before issuing any entry-cancellation side effect. A failed/uncertain fence commit must not start cancellation.
2. Every effect path must consult the same current durable per-instance fence at its effect boundary. The owner policy then blocks new-risk effects during SUSPEND while permitting independently verified protective/reduce-only effects and authoritative-outcome-only entry cancellation. Runner must check the fence before effects, including continuations/retries, and downstream Paper, cancellation, protection, fill and recovery/replay writers must either enforce the same revision or receive a verifiable owner-issued decision. An admission-only check in `dispatch_plan()`, UI-only gate, or process-local flag is insufficient.
3. Keep cancellation intent pending until an authoritative, identity-bound cancellation result is recorded. Do not treat a request acknowledgement, timeout, missing response, or stale projection as proof of cancellation.
4. If an entry's outcome is uncertain, preserve its reservation/exposure accounting, set qualification to `RECOVERY_REQUIRED`, and block new risk until authoritative reconciliation. Do not release capacity based on an assumed cancellation.
5. Continue only independently verified protective/reduce-only actions under the selected policy. The trusted source and verifier for “protective,” “reduce-only,” and “verified” are unresolved and must be owner-specified; no action may be classified from a caller label alone.
6. RESUME is an explicit operator command committed with compare-and-swap against the current lifecycle revision and exact strategy/package identity. Its qualification receipt must bind source revisions/generation orders, positions, protection, plan, and data; commit must verify that every bound version is still current, preventing a time-of-check/time-of-use race. Stale identity/revision/source, failed validation, or uncertain cancellation rejects the command and leaves the risk gate closed. The freshness rule and evidence authorities remain unresolved.
7. A monotonic owner generation/high-water value must not roll back through ordinary restore or checkpoint recovery. If current generation/high-water cannot be proven, retain the closed gate; do not infer freshness from restored state.
8. Restart and recovery never clear the fence or auto-resume. Do not reuse Runner `PAUSED`, Kill Switch, or STOP as the strategy-level risk gate.

These are architecture-derived requirements for the proposal, not evidence that current Runner, Supervisor, or Control Plane code enforces them. Sol's final review specifically requires acceptance cases for the SUSPEND/dispatch race, commit-time staleness of each RESUME receipt version, and rollback through restore/checkpoint, in addition to duplicate/replay, stale-CAS, concurrent-writer, uncertain-cancellation, recovery, and protective-action cases. These remain proposal requirements; no owner contract, implementation, or authority is accepted here.

## Transition matrix proposal

No row below is accepted until the owner confirms the command authority, guards, side effects, and recovery policy. Skip transitions only when an explicit owner-approved rule names them.

| Current state | Command / evidence | Preconditions to verify | Next state | Owner decision still required |
|---|---|---|---|---|
| None | Register exact candidate/package identity | Candidate identity and package artifact are immutable and resolve to the same strategy | `Candidate` | Whether incomplete/rejected candidates may be recorded and how legacy records enter |
| `Candidate` | Record Auditor result | Audit decision is verified and bound to the exact package/candidate; failed or unavailable decisions cannot advance | `Audited` | Which audit outcomes map to `Audited` versus a separate rejected/withdrawn record |
| `Audited` | Accept for PAPER | Required robustness/holdout and PAPER-admission evidence verifies against the exact package; LIVE authorization is absent | `PaperAccepted` | Whether acceptance requires a separate trusted actor/policy decision in addition to package admission |
| `PaperAccepted` | Bind/activate | Bind the selected immutable package to an instance and runtime; verify current safety verdict, runtime preflight, and admission evidence | `Running` or retained `PaperAccepted` | `Running` means authorized/bound; observed Runner status is separate. Selected membership is one package to many independent instances and one runtime to many instances; binding mechanics remain open. |
| `Running` | Suspend | Owner-selected behavior: block new risk; do not auto-close positions; continue verified protective/reduce-only actions. Draft interpretation: commit durable risk fence before sending identity-scoped cancellation requests; keep pending state/reservation until authoritative per-order outcome (ACK/timeout is not completion). Validate this request/result distinction at contract acceptance. | `Suspended`, with `risk_gate=SUSPENDED` and qualification reflecting verified, cancellation-pending, or recovery-required state | Lifecycle/evidence authority, durable store/fence, trusted cancellation and protective-action receipts, uncertain-outcome reconciliation, and exact evidence authorities remain unresolved |
| `Suspended` | Resume | Explicit operator command; CAS against current revision and exact strategy/package identity; direct canonical-owner checks and commit-time currentness recheck; fresh plan/data/protection validation; no unresolved cancellation/recovery | `Running` (governance authorization/binding, not a Runner execution assertion) after successful commit | Governance target is `Running` (authorized/bound), not a Runner execution assertion. Trusted operator/auth authority, concrete source/verifier identities, binding authority and re-binding sequence when current binding is absent, and implementation remain open. Reuse prior audit/PAPER admission only while current and unrevoked; repeat admission after revoke/expiry or a new package. Missing sources fail closed. |
| `PaperAccepted`, `Running`, or `Suspended` | Supersede with a new immutable identity | Replacement identity is explicit; old and new records remain independently addressable | `Superseded` for old identity | Allowed source states, atomicity across identities, and runtime stop/flatten ordering |
| `Superseded` | Archive | Retention/evidence obligations are satisfied and no runtime binding or unresolved operation remains | `Archived` | Archive prerequisites, retention, and legacy-package handling |
| `Archived` | Any activation/resume | Not allowed; create a new immutable identity for a new approval | No transition | Confirm retention and access policy; archive never reactivates |

The lifecycle owner must reject implicit transitions and preserve the previous revision and evidence chain. Runner and Supervisor events may be inputs to a transition only through typed owner evidence and the approved policy; they do not independently mutate governance state.

## Commands, revisioning, and idempotency

The authoritative store commits each command intent and result with a monotonically increasing lifecycle revision. Identical command ID plus identical payload replays the recorded result without repeating an external side effect. Reusing an ID with another payload is a conflict. A stale expected revision is rejected without mutation.

If an external Runner effect may have happened but its result was not durably recorded, the owner enters a reconciliation-required condition. It must inspect authoritative Runner state and the binding identity before accepting another effect. It must never automatically retry an uncertain stop/start or infer success from a process-local lock.

Cross-process fencing must be enforced by the authoritative store and checked at runtime admission/continuation boundaries. A Python mutex or single-process assumption alone does not satisfy #395. The Runner remains the authority for whether an operation completed.

## Durability, replay, and recovery

The transition log must survive owner restart with identity, actor, policy, revision, evidence, intent, result, and timestamps intact. Recovery must preserve a suspension and all uncertain outcomes. Stale evidence, missing stores, corrupted records, unavailable owner evidence, stale revisions, and fencing failures fail closed and remain inspectable.

No automatic migration may invent a prior approval, audit linkage, active binding, or owner attribution. Legacy accepted packages require an explicit migration mapping and verifiable provenance before they enter this state model.

## Open owner decisions

The owner must provide explicit, reviewable answers for the following before schema or implementation work:

1. Identify the concrete Strategy Lifecycle service/process and accountable operator, trusted command boundary, deployment owner, and retention operator; select the Core ledger path/operator and establish the required backup/restore policy for the owner-approved local SQLite design.
2. `Running` and the RESUME governance target are selected as authorized/bound; observed Runner status is separate. Define trusted operator/auth and binding authority, including re-binding order when current binding is absent.
3. Define who may revoke or expire package/audit/runtime evidence and which policy version applies.
4. RESUME currentness is selected as direct canonical-owner checks plus commit-time currentness recheck; missing sources fail closed. Name canonical source/verifier owners for plan/data/safety, order inventory and terminal cancellation/flat reconciliation, position/protection, and effect classification; specify freshness, expiry, and revocation.
5. Define how the proposed `risk_gate` and `qualification` fields map into the accepted governance state and how the durable fence is stored, versioned, propagated, and enforced across every Runner/Paper/cancellation/protection/fill/recovery effect boundary.
6. Define how Supervisor HALT affects RESUME eligibility without conflating states. Reuse prior audit/PAPER admission only while current and unrevoked; repeat admission after revoke/expiry or for a new package. RESUME still requires explicit operator intent and fresh plan/data/protection checks.
7. Define supersession source states, atomicity, and runtime side-effect ordering. Replacement is owner-selected to wait until open orders and positions reconcile flat; define the authoritative sources and flat predicate.
8. Define archive prerequisites, retention, access, and permanent non-reactivation semantics.
9. Define migration/provenance requirements for previously accepted packages and recovery of incomplete/uncertain historical operations.

Answers must identify the decision, exact values and source evidence, approving GitHub identity, and UTC timestamp; unresolved fields remain explicit and block implementation.

## Acceptance evidence

After owner approval and implementation, #395 needs exact-head evidence for duplicate replay, payload collision, stale revision, concurrent writers, cross-process fencing, unauthorized actor, revoked/expired/tampered evidence, uncertain Runner outcome and reconciliation, restart with suspended/in-progress state, supersession, archive non-reactivation, and legacy migration. The read-only Control Plane projection must show governance state, binding identity, Runner state, Supervisor verdict, revision, and evidence freshness separately. PAPER-only enforcement and no Core lifecycle ownership must remain intact.

## Source evidence reviewed

- Strategy package, audit, and PAPER admission facts: `research/strategy_factory/src/strategy_factory/contracts.py` and `artifacts.py`.
- Runtime binding is explicitly partial and is not runtime/PAPER qualification: `research/strategy_factory/src/strategy_factory/runtime_binding.py`.
- Runner owns its PAPER-only START/STOP/kill-switch/reset operations and durable operator replay; its contract assumes one owner process and does not provide cross-process fencing: `applications/trading_runner/src/modular_trading_runner/operator_lifecycle.py` and `docs/trading_runner/OPERATOR_LIFECYCLE_CONTRACT_V1.md`.
- Risk Supervisor owns safety verdict/HALT/recovery and gates risk-increasing plans: `applications/risk_supervisor/src/modular_risk_supervisor/`.
- Control Plane Strategy projection reads Factory run checkpoints/status: `applications/control_plane/src/modular_control_plane/strategy.py` and `adapters/strategy.py`.
- Coordinating requirements: #381 and #379 are closed; #380 is open. #395 remains open and all ten deliverables are unchecked.
