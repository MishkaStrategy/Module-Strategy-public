from __future__ import annotations

from modular_core import ModuleManifest

MODULE_NAME = "paper_execution_venue_reconciliation"
MODULE_VERSION = "0.1.0"
API_VERSION = "1.0"


def build_manifest() -> ModuleManifest:
    return ModuleManifest(
        name=MODULE_NAME,
        version=MODULE_VERSION,
        api_version=API_VERSION,
        required_timeframes=(),
        required_features=(),
        optional_features=(),
        produced_features=(
            "execution.paper.result.v1",
            "execution.paper.reconciliation.v1",
            "execution.paper.recovery.v1",
        ),
        warmup_bars={},
        parameters_schema={
            "execution_mode": {"type": "string", "default": "paper"},
            "reconciliation_mode": {"type": "string", "default": "strict_block"},
            "strict_invariants": {"type": "boolean", "default": True},
        },
        skip_until_ready=False,
        description=(
            "Paper-only execution orchestration over Core execution contracts with "
            "deterministic intake, venue normalization and fail-closed reconciliation."
        ),
        state_schema_version="1",
    )
