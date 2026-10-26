# GitHub Actions fan-out finalization — 2026-08-09

Repository: `MishkaStrategy/Module-Strategy`

Canonical integration checkpoint for this finalization:

`integration/modular-strategy-full-rc@4412407cdfe43c7529d7b1e680e78d8a334ba54a`

This document is the final addendum to `CI_FANOUT_AUDIT_2026-08-09.md` and the fan-out-specific synchronization layer for the older `WORKFLOW_INVENTORY.md` baseline. The full inventory remains useful as the 2026-08-01 historical map; for the workflows listed below, this document is the newer routing truth.

## Completed slices

| Scope | Issue / PR | Integrated result |
|---|---|---|
| Legacy Decision Router + Signal Bot/Core/Factory path routing | #472 / #473 | unrelated PRs no longer create the two legacy Router shells; Signal Bot-only owner paths no longer trigger Core/Strategy Factory solely from those paths |
| No-code Draft/Ready reruns | #477 / #480 | Strategy Factory and legacy Router gates no longer rerun solely on Draft/Ready state changes with unchanged candidate SHA |
| Legacy external-corpus shared-policy trigger | #475 / #476 | shared runner-policy edits no longer create the branch-gated external-corpus registration shell |
| Research Heavy/Release/Security dispatcher | #482 / #485 | three independent opt-in PR workflow shells consolidated behind one `research-runner-validation` dispatcher; selected labels still rerun on every synchronize |
| CI-policy tests vs Core PR routing | #484 / #495 | CI-only regressions moved under `tests/ci_policy/**`; Fast and runner-policy still execute them; Core PR matrix is not triggered solely by that namespace; integration/main push coverage remains conservative |
| Residual Research `full-integration` label shell | #496 / #507 | ordinary `research-runner-ci` no longer subscribes to arbitrary `labeled`; `full-integration` is owned by the single dispatcher and calls the reusable full matrix |

## Current fan-out-sensitive workflow inventory

| Workflow | Current PR/event role | Fan-out invariant |
|---|---|---|
| `decision-router-final-gate.yml` | automatic only on Router-owned PR path surface; exact candidate changes remain covered | unrelated PRs do not create a skipped Router shell |
| `decision-router-core-v042-compatibility.yml` | same legacy Router owner surface | unrelated PRs do not create a skipped compatibility shell |
| `market-data-external-corpus-registration-ci.yml` | legacy owner paths plus manual dispatch; owner-branch job guard retained | generic runner-policy changes do not create the 480-minute registration workflow shell |
| `ci.yml` | Core PR validation plus integration/main push | `tests/ci_policy/**` is excluded only as a sole PR trigger; real Core/shared changes and integration/main push remain covered |
| `strategy-factory-ci.yml` | Strategy Factory owner/shared source plus integration/main coverage | Signal Bot-only source is not a sole PR trigger; unchanged-SHA Draft/Ready transitions do not relaunch the matrix |
| `fast-checks.yml` | recursive fast policy/correctness gate | still executes `tests/ci_policy/**`; no policy test was deleted |
| `runner-routing-policy.yml` | centralized workflow/runner routing policy | explicitly executes the namespaced CI-policy regressions |
| `research-runner-ci.yml` | ordinary Research opened/reopened/synchronize fast validation; ready/manual full entrypoint | no generic `labeled` subscription; no `full-integration` label logic remains here |
| `research-runner-full.yml` | reusable/manual full Python 3.12/3.13 validation | preserves exact-head, authoritative Backtest/Results Aggregator, >=90% branch coverage, wheel/smoke/repro, privacy and evidence upload |
| `research-runner-validation.yml` | single `labeled`/`synchronize` dispatcher | owns `full-integration`, `heavy-validation`, `release-validation`, `security-validation`; selected validation reruns on every new head |
| `research-runner-heavy.yml` | reusable/manual | no independent PR event shell |
| `research-runner-release.yml` | reusable/manual | no independent PR event shell |
| `research-runner-security.yml` | reusable/manual | no independent PR event shell |

## Final measured/verified outcomes

The workstream deliberately optimized workflow creation/routing rather than deleting validation.

Confirmed evidence during the work:

- Signal Bot baseline sample had 10 workflow runs; first-wave routing removed the two Router shells and the owner-path-only Core/Factory fan-out while retaining Signal Bot owner, executable, Fast and central policy gates.
- A Draft->Ready probe on the accepted no-code-transition fix created zero new Strategy Factory/Router runs on the unchanged SHA.
- External-corpus trigger validation showed Router and Strategy Factory absent and removed the generic policy-file dependency from the branch-gated corpus workflow.
- Research validation consolidation replaced three independent Heavy/Release/Security PR workflow records with one dispatcher workflow while preserving label-selected exact-head reruns.
- CI-policy isolation keeps policy tests blocking in Fast/runner-policy but no longer makes the Core 3.12/3.13 PR matrix run merely because a CI-policy regression lives under root `tests/**`.
- Residual `full-integration` consolidation removes the second ordinary Research workflow shell on unrelated Research validation labels. Pre-integration validation passed Fast, actionlint, runner/environment policies and the full ordinary Research quality/build/repro/privacy path; artifact upload alone was blocked by the repository-wide storage quota.

## Corrected evidence interpretation

Issue #502 was closed `not_planned` and produced no code changes. The earlier claim of approximately sixty **integration-push** workflows from merge SHA `61e000caf7eab35aa68c3995c2bca312c0ea58b2` was invalid because the connector used for that measurement returns pull-request-triggered workflow runs. No push-fan-out conclusion is carried forward from that evidence.

Any future push-fan-out optimization must first prove `event=push` with an event-aware source. Do not infer push fan-out from commit-associated PR workflow records.

## Process improvement learned during the audit

Technical reverse-sync PRs can themselves create PR-event fan-out. For non-conflicting feature refreshes, a history-preserving two-parent merge commit built from the canonical full-RC tree plus the feature blobs can avoid an otherwise unnecessary temporary sync PR while keeping ancestry intact. The resulting feature head must still receive exact combined-tree acceptance. Never use force-push/rebase as a substitute for this requirement.

## Gates intentionally preserved

The completed fan-out changes do not lower or remove:

- owner-specific component CI;
- Core Python 3.12/3.13 correctness when Core/shared changes apply;
- Strategy Factory correctness/security/package gates when its owner surface applies;
- Fast checks;
- actionlint;
- runner/environment dependency policies;
- immutable action/setup-uv policies;
- Research >=90% branch coverage;
- authoritative Backtest/Results Aggregator checks;
- reproducible build and clean/smoke package checks;
- evidence privacy/sanitization;
- exact-head reruns after candidate changes;
- manual heavy/external-corpus validation;
- integration/main validation coverage.

## Residual operational limits outside #472

1. Shared self-hosted `fast` capacity can still cause long queues. Fan-out reduction lowers avoidable demand but does not add runner capacity.
2. GitHub evaluates PR `paths` over the cumulative PR diff; a PR that modifies workflow files can legitimately keep retriggering those workflows on later synchronize events.
3. Artifact storage quota is tracked separately by #390. Multiple otherwise-green jobs currently fail only at `upload-artifact`; no fan-out fix weakens evidence policy to hide that infrastructure condition.
4. Future fan-out work requires fresh event-aware measurements; this workstream does not claim that every repository workflow is globally optimal.

## Closure decision for #472

All proven PR-shell/matrix causes identified by #472 and its accepted follow-ups are now integrated. The historical audit is preserved, this final addendum records the accepted end state, and the affected workflow inventory is synchronized without pretending that unrelated workflows were re-measured.

#472 can be closed as completed. New fan-out findings should use a new bounded issue backed by event-aware evidence rather than reopening this audit.
