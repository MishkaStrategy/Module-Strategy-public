# #778 System Recovery Profile and Control Plane Workflow — ADR Draft

**Status:** Owner-approved safety boundary for the fail-closed local reference. Revision 637 corrects the serving-state description against the dirty local candidate at Full-RC base `127c483659fbb3acdfc90ba39ecb3af2b4c69616`: its Control Plane server mounts the bounded recovery router, while the recovery service defaults to unconfigured. Full profile/policy/source acceptance remains unresolved, #778 is OPEN and incomplete, and no owner-backed production source is configured.

**Scope:** Records the owner-approved safety contract and bounded disabled local reference for a named recovery-profile service and Control Plane (CP) workflow. It grants no production or workload authority.

## Context and gap

The accepted #377 System Recovery work supplies typed backup, manifest verification, restore, and
owner-native post-restore primitives. The local recovery suite includes clean-host restoration
proofs, including the Trading Runner durable set, Core/OMS/Paper state, Signal Bot, and other
owners. The roadmap marks #778 OPEN: no owner-backed named recovery-profile service or production
source configuration exists. In the dirty local candidate, `create_server()` mounts a bounded
recovery router and accepts optional `recovery_profile_service` injection; without an injected
service, an authorized request fails closed with `503 CONTROL_RECOVERY_NOT_CONFIGURED`. The router
is therefore present in that local serving candidate, but recovery capability is not owner-backed or
enabled, and no recovery effect can run. The missing accepted layer is the owner-approved
profile/catalog, readiness and authorization sources plus durable intents, reconciliation, and
trusted evidence around the existing primitives; this work does not replace factual owners or
introduce a second runtime source of truth. The
#377 proofs cover their versioned owner inventory only; they do not automatically prove future
#933/#396 composition or recovery coverage for owners added later.

Evidence in the current code worktree includes:

- `applications/system_recovery/src/system_recovery/package.py:35-54, 97-115` defines typed
  `SnapshotRequest`, package creation, and manifest loading against a separately trusted digest.
- `applications/system_recovery/src/system_recovery/composition.py:16-73` verifies source and
  platform identity, preflights every embedded payload and destination, rejects partial roots,
  restores in canonical order, and rolls back files published by a failed attempt.
- Clean-host owner proofs include
  `applications/system_recovery/tests/test_trading_runner_restore.py:319-345`,
  `test_core_paper_restore.py:286-310`, and
  `test_signal_bot_restore.py:124` (plus other owner restore suites).
- `applications/control_plane/src/modular_control_plane/server.py:704-708` currently mounts the
  Paper lifecycle router with its operator authorizer. This is a transport/auth pattern to review,
  not an existing recovery adapter or an authorization decision for recovery.
- In the dirty #778 local candidate, `server.py:742-748` mounts
  `create_recovery_profile_router()` with explicit reader/operator authorization callbacks. The
  recovery service is injectable but defaults to absent; authorized calls without it return a
  sanitized 503. This proves a local transport seam only, not owner-backed source readiness,
  durable replay, trusted evidence, or enabled backup/verify/restore effects.
- HQ `ROADMAP.md` §F/G and `RELEASE_CHECKLIST.md` record #778 as open and require a bounded
  owner-approved recovery contract and candidate-bound recovery evidence.

## Local reference status (revision 565; 2026-10-23)

The code worktree adds `applications/control_plane/src/modular_control_plane/recovery_profiles.py`,
`recovery_profile_api.py`, and `applications/control_plane/tests/test_recovery_profile_api.py`.
The typed models define strict frozen backup, verify, and restore intent bindings. The
dependency-injected resolver checks the complete request fingerprint and recovery authorization,
profile version, full canonical owner set, owner readiness/capture boundary, and either a backup
proposal or existing package evidence. It creates only an in-memory typed intent and returns a
sanitized operation/fingerprint summary; it has no intent store, ledger, durable replay guarantee,
or effect executor. Its capability contract fixes recovery effects, execution admission, and a
durable intent store to `false`.

The router factory requires explicit recovery-specific reader/operator authorizers, applies a 16
KiB POST body cap before JSON parsing, and returns sanitized generic failures. `server.py` does
not mount it; the default service is unconfigured. The current serving Control Plane therefore
has no recovery endpoint, and no real catalog, authorization, readiness, proposal, or package
evidence source is configured. The owner confirmed there are no real non-secret configuration
references or assigned owners/admin; those inputs remain fail-closed. Local focused tests passed
again on 2026-10-23 (87 tests in the current suite); earlier Ruff check/format, strict MyPy,
whitespace/diff checks, Graphify update, and GPT-6.1 Sol read-only review found no P0-P2 for the
then-current local diff. These local checks do not establish source trust or owner acceptance.

This is an owner-approved safety boundary and local disabled reference only. It does not complete
#778 or resolve the remaining fail-closed decisions: trusted manifest-digest root/evidence channel;
assigned readiness/auth/profile owners and real source references; versioned profile owner
inventory; owner-consistent capture and restore fencing/generation; durable intent/replay/
reconciliation; trusted post-backup digest publication; binding backup destinations to an
owner-approved profile target; owner-native reopen/reconciliation; and complete Dashboard plus
`modular-control`/CLI parity, sanitized evidence, and real-loopback E2E.


These are substrate and owner-native restore proofs. They do not establish named-profile selection,
operator authorization, request idempotency, recovery status visibility, or a CP-to-recovery
workflow.

## Proposed boundary

The proposed service accepts a stable, owner-defined `profile_id` and a caller-supplied unique
`request_id`, with only other explicitly approved scalar identity fields if owners require them.
The API must not accept a browser/API-controlled filesystem path, host, URL, command, or arbitrary
restore target. A server-side, owner-owned allowlist maps each approved profile to typed
`SnapshotRequest` values and typed restore-target references. The allowlist contents and target
mapping remain unresolved owner inputs. Until that contract is accepted, any recovery UI may expose
only factual, read-only capability/readiness information; it must not synthesize recovery state or
disclose filesystem paths.

Before capture, each owner must identify a consistent factual boundary for its snapshot set. The
profile must not capture a mixed checkpoint/journal/replay state while owner mutations can cross the
capture boundary. The capture quiescence or fencing mechanism, affected owners, and proof remain
owner decisions.

Before an effect, the service must obtain explicit readiness and authorization decisions from
owner-approved sources. Missing, stale, ambiguous, unavailable, or malformed evidence fails closed
before mutation. The request must not treat caller-supplied readiness, actor identity, approval, or
target data as authoritative. The authoritative sources, freshness rules, and authorization
boundary require owner selection; this draft sets no defaults.

Before restore, acquire an exclusive lifecycle/fence for the typed restore target. While holding it,
revalidate authorization, readiness, and the target generation, and reject conflicting requests or
stale writers before mutation. A partial restore leaves the affected owner unavailable for execution
until reconciliation and owner verification complete. Lock ownership, fencing tokens, target
generation authority, and failure recovery remain unresolved owner choices.

Persist an immutable operation-specific intent before effects. A backup intent freezes the
operation kind, versioned profile definition, candidate/source commit and platform release
identity, owner set and owner-consistent capture boundary, new package identity, typed destination
identity/generation, and authorization evidence. It cannot bind a manifest digest that does not
exist yet. After package creation, record the observed package identity and manifest digest in an
independently trusted evidence channel before reporting backup success. The trust root and evidence
store/channel are unresolved owner decisions.

A restore or verification intent, by contrast, binds an already existing package identity and its
independently trusted expected manifest digest before the effect, together with the versioned
profile definition, source/release identity, owner set, applicable typed restore-target
identity/generation, and authorization evidence. Exact request replay uses the frozen intent and
package binding; it must never silently resolve a newer “latest” profile mapping or package. Reusing
the identity with conflicting content must be rejected. Persist observed results and evidence
correlation afterward. If an effect or evidence write has an ambiguous outcome, retain a
recovery-required/uncertain state and reconcile before retry; do not blindly repeat an uncertain
restore or imply that a timeout means no effect occurred. The durable store, transaction guarantees,
reconciliation source, and manual-intervention path are owner decisions.

The workflow may report restore success only after every declared owner reopens the restored state
and completes its required reconciliation and verification; restored bytes or a manifest alone are
insufficient. Exact restoration of captured owner-native orders, positions, and schema-backed cash,
reservations, and replay state is in scope when explicitly declared by the accepted versioned
profile. Such restoration recovers recorded facts; it must not dispatch new work, create new
economic effects, flatten/cancel state, fund accounts, or resume/restart execution. Execution
admission is a separate decision by its existing authority and is never implied by recovery success.
The verified result and evidence must also be visible through the approved operator surface.

## Owner decisions still required

The following are intentionally unresolved. No profile, threshold, identity source, or policy is
selected by this draft:

1. The allowed profile IDs, request types, owner set, and eligible recovery/backup targets.
2. The authoritative readiness source for each profile, its freshness requirements, and behavior
   when a source is unavailable or contradictory.
3. The actor identity, approval requirement, and authorization boundary, including which owner
   authorizes each recovery stage.
4. The recovery stages and terminal proof required to declare completion, including post-restore
   owner verification and operator-visible recovery status.
5. The binding between a profile, source/platform identity, typed restore target, and manifest;
   the independent trust root and evidence store/channel for expected manifest digests; retention
   ownership and policy; and rules for target reuse or replacement.
6. Failure, timeout, partial effect, retry, reconciliation, and manual-intervention semantics,
   including how uncertain outcomes remain fenced from duplicate effects.
7. The owner-consistent capture/quiescence boundary, exclusive restore-target lifecycle, fencing
   and generation source, stale-writer rejection, and when partial restore makes an owner
   unavailable.
8. The versioned candidate/profile owner inventory, including the exact owners whose state and
   post-restore checks are covered. Existing #377 proofs do not automatically cover future #933/#396
   composition or new owners.

Until these choices are made by the appropriate owners, unsupported or unknown requests and
readiness states must fail closed. The service must not infer defaults from the current operator,
host, filesystem, runtime, or available backup contents.

## Proposed acceptance sequence and evidence

1. Before the full profile contract exists, keep any recovery UI factual and read-only: show only
   owner-approved capability/readiness facts, with no synthetic state or filesystem-path disclosure.
2. Owners inventory the versioned candidate/profile owner coverage and resolve the decisions above,
   then accept a versioned profile/request contract and explicit CP authorization/readiness boundary.
3. Implement the owner-approved named-profile catalog and request state using the existing
   typed #377 primitives; keep owner databases and runtime state authoritative. Define durable
   intent/result and idempotency/reconciliation behavior, distinct pre-effect backup versus
   restore/verify bindings, the post-creation trusted digest record required before backup success,
   capture boundary, and exclusive target fence before wiring effects. The current value models do
   not provide persistence or these guarantees.
4. After owner sources and durable intent behavior are selected, enable and integrate a CP
   adapter that accepts only approved stable identifiers, returns a correlated recovery status, and
   exposes no caller-controlled path, host, URL, command, or arbitrary target. The mounted local
   router is not acceptance-ready: it has no owner-backed production service, profile/readiness
   sources, durable intent store, or trusted evidence configuration.
5. Verify complete backup/restore evidence and owner-native reopen/reconciliation before a
   terminal-success response. Keep execution admission separate. Make uncertain, partial, and failed
   states visible to the authorized operator.
6. Add synthetic, temporary-fixture tests for: unknown profile/package and malformed/missing/stale
   readiness or authorization denied before mutation; request replay with exact and conflicting
   payloads; intent persisted before effects; failure/timeout after an effect reconciled without
   blind repeat; backup success withheld until the created package identity and manifest digest are
   recorded in the independently trusted evidence channel; source/release/manifest mismatch,
   tampered payload, partial root, stale target generation, and conflicting writer rejected or
   fenced before unsafe mutation; clean-host restore followed by owner reopen/reconciliation;
   incomplete evidence never reported as success; and no dispatch, flatten, funding, resume, or
   execution-admission side effect. Include unauthorized requests and tests that sanitized
   responses/evidence never leak filesystem paths.
7. Verify Dashboard and `modular-control` parity for the approved recovery status/actions, sanitized
   manifest digest plus source/release identity, and a real-loopback end-to-end flow. The exact E2E
   environment, evidence format, and thresholds remain owner choices; this draft supplies none.
8. Review the composed implementation and candidate-bound evidence with the owners. Only explicit
   acceptance can close #778; passing local tests or the existing #377 clean-host proofs alone
   cannot establish future candidate/profile or owner coverage.

This is a proposed acceptance plan. The disabled local intent-model/router reference described
above is implemented, but no enabled service, effect, owner-selected profile policy, or acceptance
step beyond the existing #377 substrate is complete.

## Authority and non-goals

The proposed recovery domain is PAPER/research state only; this is not permission to execute PAPER.
LIVE/private-exchange recovery is out of scope. This proposal does not modify, replace, or absorb the
#775/#777 ancestry synchronization work and does not claim recovery of a complete candidate. It
grants no authority to run SEARCH, HOLDOUT, PAPER, soak, CI, deployment, or release work. It does not
authorize GitHub changes, production provisioning, candidate execution, destructive retention, or
changes to owner policies. It supplies no enabled or production adapter and does not mark #778 complete.
