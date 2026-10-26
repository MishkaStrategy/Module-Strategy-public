# Full Project Integration Ledger

Status: **MULTI-STRATEGY BETA / MS5 FAILED / BOUNDED REPAIR OPEN**.
Live GitHub audit: 2026-10-02. This is a projection of the source checkpoint in
`PROJECT_STATE.yaml`, not an independent machine-readable truth store.

- implementation branch: `integration/modular-strategy-full-rc`;
- observed source: `30bef03c8ea6a99fb202a5f93a63c9afc22516f3`;
- observed HQ/default main: `eb4ef89a3c477e689e689da75cb3fd9ad94c7b4b`;
- product direction: owner-approved multi-strategy beta #1193;
- historical successor #250/#938: STOPPED by #1190 Option 2;
- Core 0.4.2 remains frozen; paper/research only.

Sequencing: [ROADMAP](../../ROADMAP.md) and
[HQ critical path](../../.github/HQ_CRITICAL_PATH.md).
Evidence: [release-readiness audit](../audits/RELEASE_READINESS_2026-10-02.md).

## Accepted boundaries

| Workstream | Verified disposition |
|---|---|
| Control UI #371 | Merged; merge commit `bcc3e33f2c99782d31441d3b44b487acdaf92d4f` |
| Recovery #377 | Closed/accepted; owner-native composition preserved |
| Observability #378 | Closed/accepted, including Web/CLI |
| Risk Supervisor #379 | Closed/accepted; full-candidate physical soak is separate |
| Storage #390 | Closed/accepted; residual watches do not reopen implementation |
| Governance #389/#984 | Closed/merged; state refreshed by this audit |
| Historical #478 first gate/#1002 | Accepted; funding/mark scenarios remain separate |
| MS0–MS4 | Accepted parity, neutral planner and technical smoke |
| #968/#973 | Closed provenance/routing blockers; occupied v3 unchanged |
| #963 | Old replication plan cancelled/superseded; do not execute |

## Residual work

MS5A throughput repair #1364/#1365 is internally merged on
`fix/ms5a-throughput-1364@41953ec0c4001ad1d600a401cd070e12380f52c3` but is not
integrated into full-RC; automatic Backtest/Research CI remains authority-gated.
Token-safe long-run handoff #1351/#1352 is accepted in full-RC; residual failure-path
terminal evidence is tracked by owner-gated #1367 and still precedes fresh exact-source
preflight and separate #1223 SEARCH authority. Last run
`36810932751` failed; authority `5924164077` is consumed and evidence
assembly/publisher were skipped. MS6–MS9 require a new multi-family lineage
and their own governance decisions.

Release lane #948/#982 is open; candidate-bound #387/#393/#400 acceptance is
incomplete. Operational #933/#873/#866/#369 follows approved package/provisioning;
old physical success is historical evidence, not new-candidate 24h acceptance.

Main and integration diverge: 605 main-only / 7266 integration-only commits
at the observed source pair. A future release integration needs an explicit
branch decision and fresh conflict/ancestry audit. Historical approval is not
carried forward by this ledger.

## Preserved rules

No direct main writes, #325 merge, history rewrite, automatic release/tag,
package publication, deployment, private exchange or LIVE action is authorized.
Existing runtime/accounting/risk/auth/recovery owners remain authoritative.
Unknown storage ownership never grants deletion authority.

Previous ledger: [2026-08-12 historical snapshot](../hq/history/FULL_PROJECT_INTEGRATION_LEDGER_2026-08-12.md).
Its heads/states/phase order must not be used as current execution truth.
