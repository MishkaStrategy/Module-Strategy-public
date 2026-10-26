# GitHub Actions fan-out audit — 2026-08-09

Repository: `MishkaStrategy/Module-Strategy`

This document records live workflow-run measurements and the bounded trigger-routing changes made to reduce GitHub Actions fan-out without removing correctness, coverage, security, packaging, or exact-head acceptance gates.

## Baseline context

The earlier CI optimization baseline in `docs/ci/CI_OPTIMIZATION.md` measured one exact SHA at:

- 50 workflow runs;
- 84 expanded jobs;
- 5,213 job-seconds (86.9 minutes);
- 63 artifacts / 12.16 MB.

That report already identified cumulative pull-request diffs as a major cause of repeated path-filtered workflow runs. The 2026-08-09 audit focused specifically on workflow shells and matrices that were provably unrelated to the changed owner surface.

## Live fan-out samples

### Signal Bot PR #469

Exact head `af4c112d017dc6e09dfc143873d8ef6018cf89ed` created 10 workflow runs for a Signal Bot-owned change:

- `signal-bot-control-ci`;
- `core-ci`;
- `strategy-factory-ci`;
- `v0.6-executable-acceptance-ci`;
- `Fast checks`;
- `actionlint`;
- `runner-routing-policy`;
- `Environment dependency runner policy`;
- `Decision Router final gate` — skipped;
- `Decision Router Core 0.4.2 compatibility` — skipped.

The Core and Strategy Factory workflows each expanded to Python 3.12/3.13 jobs even though the changed source/test paths were Signal Bot-owned.

### Trading Runner PR #471

Exact head `5452a2257f0642c60bcfcd3b1110b74678e5032e` created five workflow runs, including the same two Decision Router skipped shells.

### Research PR #466

Exact head `13d21b863e17cc0729e327c236dac02b31535f0d` created nine workflow runs, including:

- two Decision Router skipped shells;
- `research-runner-heavy` skipped without `heavy-validation`;
- `research-runner-release` skipped without `release-validation`;
- `research-runner-security` skipped without `security-validation`.

The three Research opt-in workflows each subscribed independently to `labeled` / `synchronize`, so an unlabeled Research head paid for three workflow records even when no specialized validation was requested.

## Confirmed root causes and remediation

### 1. Legacy Decision Router gates subscribed to every PR

The two Router workflows executed their only job only for head branch `modules/trade-plan-router-v0.5`, but their workflow-level PR trigger was global.

PR #473 added Router-owned path filters while preserving the owner-branch guard. The same PR also prevented Signal Bot-only tests/source from being the sole trigger for Core and Strategy Factory PR matrices while retaining broad integration-push coverage.

Accepted exact head: `d91093f8788d760e32e539897223c33606592588`.

Merged into full-RC as `c52c22c93ffd79b48a2f49db39795cbb8227aa36`.

Measured effect on the next CI-policy PR #476: Router workflows and Strategy Factory were absent from the initial workflow set. The PR created six workflows rather than reproducing the previous Router/Factory fan-out.

Expected Signal Bot shape after #473: approximately six workflows instead of the measured ten, while Signal Bot owner CI, executable acceptance, Fast checks, actionlint and central policy gates remain intact.

### 2. Draft/Ready transitions reran expensive checks on the same SHA

On accepted #473 head `d91093f8788d760e32e539897223c33606592588`, changing the PR from Draft to Ready created, without a tree change:

- a second full Strategy Factory Python 3.12/3.13 matrix (`31270842038`);
- a new skipped Decision Router final run (`31270842011`);
- a new skipped Decision Router Core compatibility run (`31270842017`).

PR #480 removed `ready_for_review` from Strategy Factory and removed `ready_for_review` / `converted_to_draft` from the two Router workflows. `opened`, `reopened`, and `synchronize` remain, so every new commit still receives exact-head validation.

Accepted exact head: `1584ef992f05616a23b48687d519891fae39dc71`.

All Core and Strategy Factory 3.12/3.13 jobs plus Fast, actionlint and policy gates passed. The final acceptance probe changed that exact SHA from Draft to Ready and re-read its workflow set twice: zero new Strategy Factory or Router runs were created.

Merged into full-RC as `7dada1edcc356900772701ac909daf7f9e565a35`.

### 3. Legacy external-corpus registration subscribed to shared policy files

`market-data-external-corpus-registration-ci` has a 480-minute `[self-hosted, backtester]` job that is executable only by manual dispatch or the legacy `integration/historical-data-lineage` owner branch. Nevertheless, its PR paths included generic runner-routing policy script/test files.

During #473 validation this produced repeated skipped workflow shells (`31270011502`, `31270125663`, `31270207511`, `31270440326`) on unrelated policy changes.

PR #476 removed only those two shared policy paths. The owner paths, owner-branch guard, manual exact-SHA dispatch, backtester role, corpus hash, registration semantics, privacy evidence and cleanup remain unchanged.

Accepted exact head: `79164b7287a58e732dc3daee3580431dfaad6b5a`.

Current-head Fast and Environment policy passed; predecessor workflow-equivalent actionlint and runner-routing passed, and the only subsequent source edit was Ruff formatting of the focused regression test. The unrelated Core matrix was explicitly not treated as an owner gate because it was created solely by the test file living under broad `tests/**`; that separate fan-out is tracked by #484.

Merged into full-RC as `bb230800fa9770ba4cb7e65dd30aa31f5eb6e203`.

### 4. Three independent Research opt-in PR workflows

Issue #482 / Draft PR #485 consolidates `research-runner-heavy`, `research-runner-release`, and `research-runner-security` behind one PR dispatcher.

The specialized workflows become `workflow_call` + `workflow_dispatch` surfaces. One `research-runner-validation` workflow owns `labeled` / `synchronize` and conditionally invokes the selected validation jobs. The selected labels remain checked on every synchronize, so exact-head semantics are preserved instead of weakening validation by deleting `synchronize`.

Live PR #485 routing evidence on combined-tree head `47dd5b9c268259aaae29f01ee43af492d2b0d6e9`:

- unlabeled synchronize created one `research-runner-validation` workflow (`31273220312`) with all three optional jobs skipped; the old standalone Heavy/Release/Security workflow records were absent;
- adding `heavy-validation` created dispatcher run `31273234712` with only `heavy / stress` queued;
- adding `release-validation` created dispatcher run `31273249105` with only Release Python 3.12/3.13 queued;
- adding `security-validation` created dispatcher run `31273264349` with only Security queued.

The opt-in labels were removed after routing inspection so any implementation fix does not repeatedly launch all expensive validations. Final acceptance still requires a stable-head synchronize probe with selected labels.

Expected unlabeled Research effect relative to the measured #466 shape:

- #473 removes two legacy Router workflow shells;
- #485 replaces three Research opt-in workflow shells with one dispatcher shell;
- measured nine-workflow shape therefore becomes approximately five workflows before any owner-specific changes elsewhere.

A residual label-event shell remains in the base `research-runner-ci` because that workflow intentionally owns the separate `full-integration` label and `ready_for_review` release matrix. It is not removed here because GitHub cannot filter `pull_request:labeled` by label name at the event level without restructuring that independent gate.

### 5. CI-policy tests trigger the full Core matrix

Issue #484 addresses a separate structural fan-out: CI-only regression tests currently live directly under `tests/`, while Core PR routing includes broad `tests/**`.

The staged `ci/isolate-policy-tests-484` branch moves policy-only regressions to `tests/ci_policy/**`, keeps them blocking in recursive Fast tests and `runner-routing-policy`, and excludes only that namespace from Core PR routing. Integration/main push routing remains broad. This avoids two Core matrix jobs on future trigger/policy-only PRs without deleting policy tests.

## Gates deliberately preserved

The fan-out work does not remove or lower:

- Core Python 3.12/3.13 correctness suites on applicable Core/shared changes;
- Strategy Factory Python 3.12/3.13 correctness, 25-scenario acceptance, branch-aware coverage, Bandit, deterministic source scan, wheel and clean-wheel checks;
- owner-specific component CI;
- Fast checks;
- actionlint;
- immutable Action/setup-uv/artifact policies;
- runner/environment routing policies;
- manual heavy/corpus validation entrypoints;
- exact-head reruns after code changes (`synchronize`);
- integration/main push validation.

The optimization principle is event/path routing: prevent a workflow from being created when its owner surface cannot possibly execute or when an event did not change the candidate tree. Substantive gates remain fail-closed on applicable changes.

## Remaining operational risks

1. Self-hosted `fast` capacity is still shared across many parallel PRs. During this audit, accepted checks spent material time queued even when no code failure existed. Reducing fan-out improves the queue but does not create additional runner capacity.
2. GitHub evaluates PR `paths` against the cumulative PR diff, so a PR that changes a workflow file will continue to retrigger that workflow on later synchronize events until the file leaves the cumulative diff. This is expected GitHub behavior and is not bypassed here.
3. Research dispatcher acceptance must still prove selected-label synchronize semantics on a stable final head before merge.
4. The workflow inventory document predates these changes and should be regenerated after all fan-out slices are integrated.

## Tracking

- #472 — parent fan-out audit;
- #473 — merged Router/Core/Factory path-routing reduction;
- #475 / #476 — merged legacy external-corpus trigger scope;
- #477 / #480 — merged no-code-change lifecycle rerun removal;
- #482 / #485 — Research opt-in workflow consolidation;
- #484 — isolate CI-policy regressions from Core PR fan-out;
- #390 — related Actions artifact/storage pressure;
- #373 — HQ platform gap tracking.
