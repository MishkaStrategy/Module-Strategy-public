# CI trigger evidence

This documentation-only commit follows the verified one-shot self-hosted maintenance patch that completed the Position Scaling side of `PS-CCR-001`.

Verified branch state before triggering permanent CI:

- functional PS-CCR source commit: `4553a7c4e15d793c990b6cf42f78c8b511932d85`;
- `ScalingPlanner` applies instrument `contract_size` to current/projected risk and planner-side notional/leverage gates;
- `build_risk_request` rejects a `contract_size` that disagrees with the planning context;
- `risk.request` publishes typed `position_adjustment` schema 1 while retaining matching legacy transition metadata and `valuation_price`;
- the approval module validates typed Risk Sizing adjustment identity, approved delta quantity, projected total quantity and weighted-average entry;
- legacy schema-1 RiskDecision payloads without the optional typed block remain backward compatible;
- targeted Ruff, Ruff format, strict mypy and PS-CCR contract tests passed in self-hosted workflow run `29863436018`;
- temporary PS-CCR and legacy maintenance workflows/scripts removed themselves;
- no diagnostic log is present in the Pull Request;
- no force push, merge, tag, release or history rewrite was used;
- Draft PR remains unmerged;
- the next `position-scaling-hardening-ci` matrix run is the authoritative exact-head validation for the corrected source tree.
