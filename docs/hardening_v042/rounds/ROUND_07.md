# Round 07 — fail-closed reconciliation outputs

- Goal: ensure reconciliation DTOs cannot represent contradictory or non-actionable states.
- Baseline: mismatches could be emitted with `safe_halt_required=False`; boolean flags accepted integers; duplicate/blank mismatch IDs were possible.
- Change: exact bool flags, mapping diagnostics, nonblank unique identity groups, matched/mismatch equivalence, mandatory safe halt for every mismatch, and additive `issue_count`.
- Tests: flag types, contradiction rejection, identity validation and stable issue count.
- Downstream usability: operators receive one deterministic actionable verdict.
- Compatibility: reports produced by `reconcile_execution_state` already satisfy every strengthened invariant.
