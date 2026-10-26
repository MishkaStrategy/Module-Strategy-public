# Acceptance Control — Phase 7 owner audit

Status: **OWNER CONTRACT REQUIRED**

Related: Issue #370, validation Issue #369, owner-gap Issue #382, Draft PR #371.

## Purpose

Phase 7 audited the repository before adding Acceptance controls to the unified Control Plane. The goal is to expose factual, browser-independent full-RC validation and soak state without inventing a second orchestration state machine or treating GitHub workflow UI as local runtime truth.

## Existing validation surfaces

The repository already has strong validation mechanisms:

- `.github/workflows/full-rc-release-readiness-ci.yml` fences an exact full-RC SHA and generates sanitized readiness evidence;
- `.github/workflows/full-rc-heavy-validation.yml` dispatches exact-head heavy Backtest, historical strategy, Signal Bot and registered-parity validations;
- `.github/workflows/strategy-factory-soak.yml` executes a bounded 100-process restart soak and uploads evidence;
- Issue #369 defines the Mac full-RC validation scope: deterministic full-corpus backtests, paper runtime/recovery, 2h and 24h soak, bounded resources and sanitized evidence;
- HQ GAP-002 records the remaining full-system long-running paper/recovery validation gap.

These surfaces are valid CI/validation evidence. They are **not** an application-level durable Acceptance owner.

## Missing owner contract

No accepted component currently owns a versioned durable run with all of the following application semantics:

- current acceptance run identity;
- exact full-RC, dataset, strategy and configuration identities;
- persisted phase sequence;
- active-test-time versus wall-clock elapsed time;
- 2h/24h soak state across Control Plane/browser restarts;
- restart count and checkpoint freshness;
- bounded CPU/RAM/disk samples;
- deterministic recovery after process interruption;
- idempotent operator start/stop/cancel/retry;
- owner-native audit attribution;
- sanitized evidence references/digests.

GitHub Actions run status cannot substitute for this contract because browser closure/reopen and local process recovery must resolve to authoritative application state, not inferred remote workflow presentation.

## Control UI decision

Until Issue #382 is accepted:

1. Control Plane does not create a duplicate Acceptance scheduler/state machine merely to satisfy UI requirements.
2. Dashboard must not claim that a CI workflow is the currently running local Acceptance job.
3. `ComponentId.ACCEPTANCE` may remain unavailable/degraded through the generic system overview when no owner adapter is attached.
4. No Start/Stop/Cancel/Retry Acceptance buttons are exposed without owner-native idempotency and operator audit semantics.
5. Existing GitHub/CI evidence remains usable as evidence, but not as mutable runtime truth.

## Required owner shape

Issue #382 defines the required owner contract. The eventual Control Plane adapter should be thin:

```text
Acceptance owner
  -> typed read/mutation port
  -> Control Plane adapter
  -> /api/control/v1/acceptance/...
  -> Dashboard + modular-control
```

The adapter must not execute arbitrary shell commands supplied by the browser or terminal.

## Safety

- PAPER/research only.
- No Live toggle.
- No private exchange credentials or signed private requests.
- No raw market data in Control Plane acceptance payloads.
- No host identity/private filesystem paths in UI, logs, events or evidence projection.
- Browser is never acceptance truth.
