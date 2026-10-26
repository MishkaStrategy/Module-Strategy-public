from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.fixed_workload_planner import PlannerError, plan_fixed_workloads

RUNTIME_ID = "cpython-3.12.8"
ENVIRONMENTS = [
    {
        "environment_id": "paper-vds-linux",
        "node_class": "paper_linux",
        "os": "linux",
        "architecture": "x86_64",
        "image_sha256": "1" * 64,
        "runtime_ids": [RUNTIME_ID],
    },
    {
        "environment_id": "darwin-acceptance",
        "node_class": "acceptance_darwin",
        "os": "darwin",
        "architecture": "arm64",
        "image_sha256": "2" * 64,
        "runtime_ids": [RUNTIME_ID],
    },
    {
        "environment_id": "historical-backtester",
        "node_class": "historical_backtester",
        "os": "linux",
        "architecture": "x86_64",
        "image_sha256": "3" * 64,
        "runtime_ids": [RUNTIME_ID],
    },
]
WORKLOADS = [
    {
        "workload_id": "paper-smoke",
        "workload_kind": "gate",
        "gate": "paper:runtime-smoke",
        "component_id": "trading-runner",
        "node_class": "paper_linux",
        "environment_id": "paper-vds-linux",
        "runtime_id": RUNTIME_ID,
        "required_outputs": [
            {
                "output_path": "evidence/paper.json",
                "gate": "paper:runtime-smoke",
                "component_id": "trading-runner",
                "artifact_sha256": "a" * 64,
            }
        ],
        "input_materials": [{"material_id": "paper-config", "sha256": "d" * 64}],
        "resource_policy": {"policy_id": "fixed-resource-v1", "sha256": "9" * 64},
        "network_policy": {"policy_id": "network-disabled-v1", "sha256": "8" * 64},
        "predecessor_material": None,
    },
    {
        "workload_id": "darwin-reconnect",
        "workload_kind": "gate",
        "gate": "operations:mac-service-reconnect",
        "component_id": "system-acceptance",
        "node_class": "acceptance_darwin",
        "environment_id": "darwin-acceptance",
        "runtime_id": RUNTIME_ID,
        "required_outputs": [
            {
                "output_path": "evidence/darwin.json",
                "gate": "operations:mac-service-reconnect",
                "component_id": "system-acceptance",
                "artifact_sha256": "b" * 64,
            }
        ],
        "input_materials": [{"material_id": "darwin-fixture", "sha256": "e" * 64}],
        "resource_policy": {"policy_id": "fixed-resource-v1", "sha256": "9" * 64},
        "network_policy": {"policy_id": "network-disabled-v1", "sha256": "8" * 64},
        "predecessor_material": None,
    },
    {
        "workload_id": "backtest-parity",
        "workload_kind": "historical_backtest",
        "gate": "backtest:historical-parity",
        "component_id": "strategy-factory",
        "node_class": "historical_backtester",
        "environment_id": "historical-backtester",
        "runtime_id": RUNTIME_ID,
        "required_outputs": [
            {
                "output_path": "evidence/backtest.json",
                "gate": "backtest:historical-parity",
                "component_id": "strategy-factory",
                "artifact_sha256": "c" * 64,
            }
        ],
        "input_materials": [{"material_id": "backtest-fixture", "sha256": "f" * 64}],
        "resource_policy": {"policy_id": "fixed-resource-v1", "sha256": "9" * 64},
        "network_policy": {"policy_id": "network-disabled-v1", "sha256": "8" * 64},
        "predecessor_material": None,
    },
]


def _identity(value: dict[str, Any], field: str) -> None:
    body = {key: item for key, item in value.items() if key != field}
    value[field] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate: dict[str, Any] = {
        "candidate_id": "",
        "source": {"commit": "d" * 40},
        "artifacts": [
            {"component": "trading-runner", "file": {"sha256": "a" * 64}},
            {"component": "system-acceptance", "file": {"sha256": "b" * 64}},
            {"component": "strategy-factory", "file": {"sha256": "c" * 64}},
        ],
    }
    _identity(candidate, "candidate_id")
    profile: dict[str, Any] = {
        "profile_id": "",
        "target_environments": ["paper-vds-linux"],
        "acceptance_requirements": [
            {
                "gate": row["gate"],
                "component_id": row["component_id"],
                "environment": row["environment_id"],
            }
            for row in WORKLOADS
        ],
        "python_runtimes": [{"id": RUNTIME_ID}],
    }
    _identity(profile, "profile_id")
    inventory: dict[str, Any] = {
        "inventory_id": "",
        "schema_version": 1,
        "environments": [dict(row) for row in ENVIRONMENTS],
        "resource_policies": [{"policy_id": "fixed-resource-v1", "sha256": "9" * 64}],
        "network_policies": [{"policy_id": "network-disabled-v1", "sha256": "8" * 64}],
        "workloads": json.loads(json.dumps(WORKLOADS)),
    }
    _identity(inventory, "inventory_id")
    return candidate, profile, inventory


def test_planner_builds_three_class_plan_without_authorizing_dispatch() -> None:
    candidate, profile, inventory = _inputs()
    plan = plan_fixed_workloads(candidate, profile, inventory)
    mapping = plan.to_mapping()
    assert plan.blockers == ()
    assert {row.node_class for row in plan.workloads} == {
        "paper_linux",
        "acceptance_darwin",
        "historical_backtester",
    }
    assert mapping["dispatch_authorized"] is False
    assert mapping["external_trust_established"] is False
    assert mapping["acceptance_eligible"] is False
    assert all(
        row["resource_policy"] == {"policy_id": "fixed-resource-v1", "sha256": "9" * 64}
        for row in mapping["workloads"]
    )
    assert all(
        row["network_policy"] == {"policy_id": "network-disabled-v1", "sha256": "8" * 64}
        for row in mapping["workloads"]
    )
    assert all(row["input_materials"] for row in mapping["workloads"])


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_environment", "environment_binding_missing"),
        ("missing_evidence_scope", "required_evidence_scope_missing"),
        ("missing_artifact", "required_evidence_artifact_missing"),
        ("missing_predecessor", "selected_predecessor_material_binding_missing"),
    ],
)
def test_planner_surfaces_missing_binding_blockers(mutation: str, expected: str) -> None:
    candidate, profile, inventory = _inputs()
    if mutation == "missing_environment":
        inventory["workloads"][1]["environment_id"] = "unbound-darwin"
        _identity(inventory, "inventory_id")
    elif mutation == "missing_evidence_scope":
        profile["acceptance_requirements"].pop(1)
        _identity(profile, "profile_id")
    elif mutation == "missing_artifact":
        candidate["artifacts"].pop(1)
        _identity(candidate, "candidate_id")
    else:
        compatibility = dict(inventory["workloads"][0])
        compatibility.update(
            workload_id="compatibility-check",
            workload_kind="compatibility",
            gate="compatibility:strategy-factory",
            predecessor_material=None,
            required_outputs=[
                {
                    "output_path": "evidence/compatibility.json",
                    "gate": "compatibility:strategy-factory",
                    "component_id": "trading-runner",
                    "artifact_sha256": "a" * 64,
                }
            ],
        )
        inventory["workloads"].append(compatibility)
        profile["acceptance_requirements"].append(
            {
                "gate": compatibility["gate"],
                "component_id": compatibility["component_id"],
                "environment": compatibility["environment_id"],
            }
        )
        _identity(profile, "profile_id")
        _identity(inventory, "inventory_id")
    if mutation == "missing_environment":
        with pytest.raises(
            PlannerError, match="workload and outputs must use an acceptance_darwin environment"
        ):
            plan_fixed_workloads(candidate, profile, inventory)
        return
    plan = plan_fixed_workloads(candidate, profile, inventory)
    assert any(expected in row.blockers for row in plan.workloads)
    assert plan.dispatch_authorized is False
    assert plan.acceptance_eligible is False


def test_planner_rejects_override_fields_and_duplicate_scope() -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["command"] = ["candidate-controlled"]
    _identity(inventory, "inventory_id")
    with pytest.raises(PlannerError, match="forbidden override"):
        plan_fixed_workloads(candidate, profile, inventory)

    candidate, profile, inventory = _inputs()
    duplicate = dict(inventory["workloads"][0])
    duplicate["workload_id"] = "duplicate-scope"
    duplicate["required_outputs"] = [
        {**output, "output_path": "evidence/duplicate.json"}
        for output in duplicate["required_outputs"]
    ]
    inventory["workloads"].append(duplicate)
    _identity(inventory, "inventory_id")
    with pytest.raises(PlannerError, match="duplicate workload evidence scope"):
        plan_fixed_workloads(candidate, profile, inventory)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.json",
        "/absolute.json",
        "evidence//gap.json",
        "evidence/CON.json",
        "evidence/name.",
    ],
)
def test_planner_rejects_nonportable_required_output_paths(unsafe_path: str) -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["required_outputs"][0]["output_path"] = unsafe_path
    _identity(inventory, "inventory_id")

    with pytest.raises(PlannerError, match="portable relative-path dialect"):
        plan_fixed_workloads(candidate, profile, inventory)


def test_evidence_scope_includes_runtime_id() -> None:
    candidate, profile, inventory = _inputs()
    second_runtime = "cpython-3.13.1"
    profile["python_runtimes"].append({"id": second_runtime})
    _identity(profile, "profile_id")
    inventory["environments"][0]["runtime_ids"].append(second_runtime)
    second = json.loads(json.dumps(inventory["workloads"][0]))
    second.update(workload_id="paper-smoke-py313", runtime_id=second_runtime)
    second["required_outputs"][0]["output_path"] = "evidence/paper-py313.json"
    inventory["workloads"].append(second)
    _identity(inventory, "inventory_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    assert plan.blockers == ()
    assert len(plan.workloads) == 4


def test_backtester_requires_profile_evidence_scope() -> None:
    candidate, profile, inventory = _inputs()
    profile["acceptance_requirements"].pop(2)
    _identity(profile, "profile_id")
    _identity(inventory, "inventory_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    backtest = next(row for row in plan.workloads if row.workload_id == "backtest-parity")
    assert "acceptance_evidence_scope_missing" in backtest.blockers
    assert "required_evidence_scope_missing" in backtest.blockers


def test_same_output_path_is_allowed_in_distinct_workload_bundles() -> None:
    candidate, profile, inventory = _inputs()
    second = json.loads(json.dumps(inventory["workloads"][0]))
    second.update(workload_id="paper-smoke-second-gate", gate="paper:secondary-smoke")
    second["required_outputs"][0]["gate"] = "paper:secondary-smoke"
    inventory["workloads"].append(second)
    profile["acceptance_requirements"].append(
        {
            "gate": "paper:secondary-smoke",
            "component_id": "trading-runner",
            "environment": "paper-vds-linux",
        }
    )
    _identity(profile, "profile_id")
    _identity(inventory, "inventory_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    assert not any("duplicate required output path" in blocker for blocker in plan.blockers)
    assert len(plan.workloads) == 4


def test_each_mandatory_node_class_needs_a_planned_workload() -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"] = [
        row for row in inventory["workloads"] if row["node_class"] != "historical_backtester"
    ]
    _identity(inventory, "inventory_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    assert "workload_node_class_missing:historical_backtester" in plan.blockers


def test_unplanned_profile_acceptance_scope_is_a_blocker() -> None:
    candidate, profile, inventory = _inputs()
    profile["acceptance_requirements"].append(
        {
            "gate": "operations:required-but-unplanned",
            "component_id": "system-acceptance",
            "environment": "darwin-acceptance",
        }
    )
    _identity(profile, "profile_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    assert (
        "profile_acceptance_scope_without_workload:"
        "operations:required-but-unplanned:system-acceptance:darwin-acceptance"
    ) in plan.blockers


def test_mac_reconnect_acceptance_scope_cannot_be_relabelled_linux() -> None:
    candidate, profile, inventory = _inputs()
    reconnect = next(
        row
        for row in profile["acceptance_requirements"]
        if row["gate"] == "operations:mac-service-reconnect"
    )
    reconnect["environment"] = "paper-vds-linux"
    _identity(profile, "profile_id")

    with pytest.raises(PlannerError, match="must use an acceptance_darwin environment"):
        plan_fixed_workloads(candidate, profile, inventory)


def test_mac_reconnect_output_cannot_be_attached_to_non_darwin_workload() -> None:
    candidate, profile, inventory = _inputs()
    reconnect = next(
        row for row in inventory["workloads"] if row["gate"] == "operations:mac-service-reconnect"
    )
    reconnect["node_class"] = "paper_linux"
    reconnect["environment_id"] = "paper-vds-linux"
    _identity(inventory, "inventory_id")

    with pytest.raises(
        PlannerError, match="workload and outputs must use an acceptance_darwin environment"
    ):
        plan_fixed_workloads(candidate, profile, inventory)


@pytest.mark.parametrize(
    "paths",
    [
        ["evidence/Result.json", "evidence/result.json"],
        ["evidence/a", "evidence/a/child.json"],
        ["evidence/a/child.json", "evidence/a"],
    ],
)
def test_workload_output_paths_reject_aliases_and_prefix_conflicts(paths: list[str]) -> None:
    candidate, profile, inventory = _inputs()
    workload = inventory["workloads"][0]
    original = workload["required_outputs"][0]
    workload["required_outputs"] = [{**original, "output_path": path} for path in paths]
    _identity(inventory, "inventory_id")

    with pytest.raises(PlannerError, match="path collision or file/directory prefix conflict"):
        plan_fixed_workloads(candidate, profile, inventory)


def test_workload_output_count_is_bounded() -> None:
    candidate, profile, inventory = _inputs()
    workload = inventory["workloads"][0]
    original = workload["required_outputs"][0]
    workload["required_outputs"] = [
        {**original, "output_path": f"evidence/output-{index}.json"} for index in range(257)
    ]
    _identity(inventory, "inventory_id")

    with pytest.raises(PlannerError, match="at most 256 entries"):
        plan_fixed_workloads(candidate, profile, inventory)


def test_reserved_compatibility_gate_cannot_be_downgraded_to_generic_gate() -> None:
    candidate, profile, inventory = _inputs()
    compatibility = json.loads(json.dumps(inventory["workloads"][0]))
    compatibility.update(
        workload_id="compatibility-downgrade",
        workload_kind="gate",
        gate="compatibility:strategy-factory",
    )
    compatibility["required_outputs"][0]["gate"] = "compatibility:strategy-factory"
    compatibility["required_outputs"][0]["output_path"] = "evidence/compatibility.json"
    inventory["workloads"].append(compatibility)
    profile["acceptance_requirements"].append(
        {
            "gate": "compatibility:strategy-factory",
            "component_id": compatibility["component_id"],
            "environment": compatibility["environment_id"],
        }
    )
    _identity(profile, "profile_id")
    _identity(inventory, "inventory_id")

    plan = plan_fixed_workloads(candidate, profile, inventory)
    workload = next(row for row in plan.workloads if row.workload_id == "compatibility-downgrade")
    assert "reserved_gate_kind_mismatch:compatibility" in workload.blockers
    assert plan.dispatch_authorized is False
    assert plan.external_trust_established is False
    assert plan.acceptance_eligible is False


@pytest.mark.parametrize(
    ("index", "node_class", "expected"),
    [
        (0, "acceptance_darwin", "environment_node_class_mismatch"),
        (1, "paper_linux", "environment_node_class_mismatch"),
        (2, "acceptance_darwin", "environment_node_class_mismatch"),
    ],
)
def test_planner_blocks_environment_class_substitution(
    index: int, node_class: str, expected: str
) -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][index]["node_class"] = node_class
    _identity(inventory, "inventory_id")
    if index == 1:
        with pytest.raises(
            PlannerError,
            match="workload and outputs must use an acceptance_darwin environment",
        ):
            plan_fixed_workloads(candidate, profile, inventory)
        return
    plan = plan_fixed_workloads(candidate, profile, inventory)
    workload_id = inventory["workloads"][index]["workload_id"]
    planned = next(row for row in plan.workloads if row.workload_id == workload_id)
    assert expected in planned.blockers
    assert plan.dispatch_authorized is False


def test_planner_rejects_tampered_input_identities() -> None:
    candidate, profile, inventory = _inputs()
    candidate["source"]["commit"] = "e" * 40
    with pytest.raises(PlannerError, match="identity does not match"):
        plan_fixed_workloads(candidate, profile, inventory)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("resource_policy", "resource_policy_binding_unknown_or_mismatched"),
        ("network_policy", "network_policy_binding_unknown_or_mismatched"),
    ],
)
def test_planner_requires_inventory_bound_workload_policies(policy: str, expected: str) -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0][policy]["sha256"] = "0" * 64
    _identity(inventory, "inventory_id")
    plan = plan_fixed_workloads(candidate, profile, inventory)
    paper = next(row for row in plan.workloads if row.workload_id == "paper-smoke")
    assert expected in paper.blockers


def test_planner_blocks_unknown_workload_policy_id() -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["resource_policy"]["policy_id"] = "unregistered-policy"
    _identity(inventory, "inventory_id")
    plan = plan_fixed_workloads(candidate, profile, inventory)
    paper = next(row for row in plan.workloads if row.workload_id == "paper-smoke")
    assert "resource_policy_binding_unknown_or_mismatched" in paper.blockers


def test_every_workload_requires_a_runtime_id() -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["runtime_id"] = None
    _identity(inventory, "inventory_id")
    with pytest.raises(PlannerError, match="runtime_id"):
        plan_fixed_workloads(candidate, profile, inventory)


def test_planner_rejects_missing_or_duplicate_input_material_bindings() -> None:
    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["input_materials"] = []
    _identity(inventory, "inventory_id")
    with pytest.raises(PlannerError, match="input_materials"):
        plan_fixed_workloads(candidate, profile, inventory)

    candidate, profile, inventory = _inputs()
    inventory["workloads"][0]["input_materials"].append(
        dict(inventory["workloads"][0]["input_materials"][0])
    )
    _identity(inventory, "inventory_id")
    with pytest.raises(PlannerError, match="duplicate input material_id"):
        plan_fixed_workloads(candidate, profile, inventory)
    candidate, profile, inventory = _inputs()
    inventory["environments"][0]["image_sha256"] = "f" * 64
    with pytest.raises(PlannerError, match="identity does not match"):
        plan_fixed_workloads(candidate, profile, inventory)


class _ReadOnceMapping(Mapping[str, Any]):
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value
        self.reads: dict[str, int] = {}

    def __getitem__(self, key: str) -> Any:
        self.reads[key] = self.reads.get(key, 0) + 1
        if self.reads[key] > 1:
            raise AssertionError(f"input key read more than once: {key}")
        return self.value[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


def test_planner_uses_detached_one_read_snapshots() -> None:
    candidate, profile, inventory = _inputs()
    wrapped = tuple(_ReadOnceMapping(value) for value in (candidate, profile, inventory))
    plan = plan_fixed_workloads(*wrapped)
    before = plan.to_mapping()
    candidate["artifacts"].clear()
    profile["acceptance_requirements"].clear()
    inventory["workloads"].clear()
    assert plan.to_mapping() == before
    assert all(count == 1 for source in wrapped for count in source.reads.values())


def test_planner_imports_no_execution_or_external_side_effect_adapters() -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/fixed_workload_planner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "sqlite3",
        "subprocess",
        "socket",
        "urllib",
        "httpx",
        "requests",
        "scripts.external_admission_ledger",
        "scripts.external_execution_publisher",
        "scripts.verify_external_execution_receipt",
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert not imported & forbidden
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not calls & {"claim", "dispatch", "sign_receipt", "launch_executor", "publish"}
