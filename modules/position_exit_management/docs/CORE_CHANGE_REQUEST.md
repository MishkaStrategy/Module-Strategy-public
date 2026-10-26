# CORE CHANGE REQUESTS

## Optional: expose original position risk in PositionSnapshot

- Severity: LOW / non-blocking.
- Current contract: `PositionSnapshot` exposes current `stop_loss`, but not original stop or initial risk per unit.
- Missing capability: first attachment of the module after the stop has already moved through entry cannot reconstruct original R.
- Necessity: only for late attachment without a previous module checkpoint.
- Proposed contract: optional immutable `initial_stop_loss: float | None` or `initial_risk_per_unit: float | None` on `PositionSnapshot`.
- Impact: additive field with default `None`; existing modules remain compatible.
- Migration: execution ledger populates the field when known; old checkpoints and adapters may leave it `None`.
- Contract tests: long/short initial risk, missing legacy value, scale-in policy, serialization.
- Local workaround: module captures first valid stop and checkpoints original risk; when unknowable it disables R-based actions and emits `position_exit_management.missing_initial_risk`.

No Core change is required to integrate this local release candidate.
