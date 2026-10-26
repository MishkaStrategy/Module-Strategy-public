# Known Limitations

1. **PS-CCR-001:** Risk Sizing 1.1 lacks typed whole-position adjustment semantics. Metadata is auditable evidence, not full enforcement.
2. No execution adapter is included; approved features are not exchange orders.
3. The module relies on factual Core `PositionSnapshot` reconciliation and does not manufacture fills.
4. Portfolio correlation/group risk requires a portfolio service outside this module.
5. Exchange quantity/tick rounding remains Risk/Execution responsibility.
6. Python 3.12 must be proven by self-hosted CI; local container validation used Python 3.13.5.
7. Local pip-audit could not query PyPI due DNS and must pass in CI.
8. Parameters do not prove profitability or live readiness.
9. Integration with final Risk Sizing, Protection & Target, Position Exit and executor branches remains required before strategy-level release.
