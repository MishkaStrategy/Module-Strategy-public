from modular_core import ModuleRegistry

from modular_strategy_position_scaling import (
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)

# Insert the actual RiskSizingModule between these modules.
planner = PositionScalingPlannerModule(
    enabled=True,
    allow_pyramiding=True,
    rules=[
        {
            "stage": 1,
            "mode": "pyramiding",
            "action": "add",
            "trigger_kind": "r_multiple",
            "trigger_value": "1",
            "quantity_kind": "initial_fraction",
            "quantity_value": "0.25",
        }
    ],
)
approval = PositionScalingApprovalModule()

registry = ModuleRegistry()
print(planner.manifest)
print(approval.manifest)
print("Register planner → Risk Sizing → approval; do not execute approvals directly.")
