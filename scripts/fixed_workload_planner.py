"""Pure, provider-neutral planning for fixed release workloads.

This module snapshots manifest and inventory inputs and produces deterministic
plans only. It cannot authorize dispatch, establish external trust, or execute
candidate code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

NODE_CLASSES = frozenset({"paper_linux", "acceptance_darwin", "historical_backtester"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTPUT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_WINDOWS = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.IGNORECASE
)
_RESERVED_GATE_KINDS = {
    "compatibility:": "compatibility",
    "migration:": "migration",
    "backtest:": "historical_backtest",
}
_DARWIN_ACCEPTANCE_GATES = frozenset({"operations:mac-service-reconnect"})
_DANGEROUS_FIELDS = frozenset(
    {
        "command",
        "commands",
        "host",
        "hostname",
        "runner",
        "runner_selector",
        "data_path",
        "owner_data_path",
        "resource",
        "resources",
        "resource_limit",
        "network_policy_override",
    }
)
_MAX_WORKLOAD_OUTPUTS = 256


class PlannerError(ValueError):
    """The fixed workload inventory or its source bindings are malformed."""


@dataclass(frozen=True, slots=True)
class EnvironmentBinding:
    environment_id: str
    node_class: Literal["paper_linux", "acceptance_darwin", "historical_backtester"]
    os: Literal["linux", "darwin"]
    architecture: str
    image_sha256: str
    runtime_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyBinding:
    policy_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MaterialBinding:
    material_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PlannedOutput:
    output_path: str
    gate: str
    component_id: str
    artifact_sha256: str


@dataclass(frozen=True, slots=True)
class WorkloadPlan:
    workload_id: str
    workload_kind: str
    gate: str
    component_id: str
    node_class: str
    environment_id: str
    runtime_id: str
    workload_sha256: str
    input_materials: tuple[MaterialBinding, ...]
    resource_policy: PolicyBinding
    network_policy: PolicyBinding
    required_outputs: tuple[PlannedOutput, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    candidate_id: str
    profile_id: str
    candidate_snapshot_sha256: str
    profile_snapshot_sha256: str
    inventory_id: str
    inventory_snapshot_sha256: str
    workloads: tuple[WorkloadPlan, ...]
    blockers: tuple[str, ...]
    dispatch_authorized: Literal[False] = False
    external_trust_established: Literal[False] = False
    acceptance_eligible: Literal[False] = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "profile_id": self.profile_id,
            "candidate_snapshot_sha256": self.candidate_snapshot_sha256,
            "profile_snapshot_sha256": self.profile_snapshot_sha256,
            "inventory_id": self.inventory_id,
            "inventory_snapshot_sha256": self.inventory_snapshot_sha256,
            "workloads": [
                {
                    "workload_id": row.workload_id,
                    "workload_kind": row.workload_kind,
                    "gate": row.gate,
                    "component_id": row.component_id,
                    "node_class": row.node_class,
                    "environment_id": row.environment_id,
                    "runtime_id": row.runtime_id,
                    "workload_sha256": row.workload_sha256,
                    "input_materials": [
                        {"material_id": material.material_id, "sha256": material.sha256}
                        for material in row.input_materials
                    ],
                    "resource_policy": {
                        "policy_id": row.resource_policy.policy_id,
                        "sha256": row.resource_policy.sha256,
                    },
                    "network_policy": {
                        "policy_id": row.network_policy.policy_id,
                        "sha256": row.network_policy.sha256,
                    },
                    "required_outputs": [
                        {
                            "output_path": output.output_path,
                            "gate": output.gate,
                            "component_id": output.component_id,
                            "artifact_sha256": output.artifact_sha256,
                        }
                        for output in row.required_outputs
                    ],
                    "blockers": list(row.blockers),
                }
                for row in self.workloads
            ],
            "blockers": list(self.blockers),
            "dispatch_authorized": False,
            "external_trust_established": False,
            "acceptance_eligible": False,
        }


def _canonical_bytes(value: Any, label: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PlannerError(f"{label} cannot be canonically encoded") from exc


def _snapshot(value: Mapping[str, Any], label: str) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        raise PlannerError(f"{label} must be a mapping")
    try:
        detached = dict(value)
        raw = _canonical_bytes(detached, label)
        snapshot = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (TypeError, ValueError, UnicodeError) as exc:
        if isinstance(exc, PlannerError):
            raise
        raise PlannerError(f"{label} cannot be snapshotted") from exc
    if not isinstance(snapshot, dict):
        raise PlannerError(f"{label} snapshot must be an object")
    return snapshot, raw


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlannerError(f"duplicate JSON key in snapshot: {key}")
        result[key] = value
    return result


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(row: dict[str, Any], field: str, label: str) -> str:
    identity = row.get(field)
    if not isinstance(identity, str) or _SHA256.fullmatch(identity) is None:
        raise PlannerError(f"{label} identity is malformed")
    body = {key: value for key, value in row.items() if key != field}
    if _digest(_canonical_bytes(body, label)) != identity:
        raise PlannerError(f"{label} identity does not match its snapshot")
    return identity


def _exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlannerError(f"{label} must be an object")
    dangerous = _DANGEROUS_FIELDS & set(value)
    if dangerous:
        raise PlannerError(f"{label} contains forbidden override fields: {sorted(dangerous)}")
    if set(value) != required:
        raise PlannerError(f"{label} fields differ from the fixed contract")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlannerError(f"{label} must be a nonempty string")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PlannerError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _parse_environment(value: Any) -> EnvironmentBinding:
    row = _exact_keys(
        value,
        {"environment_id", "node_class", "os", "architecture", "image_sha256", "runtime_ids"},
        "environment binding",
    )
    environment_id = _text(row["environment_id"], "environment_id")
    node_class = _text(row["node_class"], "node_class")
    if node_class not in NODE_CLASSES:
        raise PlannerError("environment node_class is unsupported")
    os_name = _text(row["os"], "environment os")
    if os_name not in {"linux", "darwin"}:
        raise PlannerError("environment os is unsupported")
    if node_class == "paper_linux" and os_name != "linux":
        raise PlannerError("paper_linux environment must declare Linux")
    if node_class == "acceptance_darwin" and os_name != "darwin":
        raise PlannerError("acceptance_darwin environment must declare Darwin")
    runtimes = row["runtime_ids"]
    if (
        not isinstance(runtimes, list)
        or not runtimes
        or any(not isinstance(item, str) or not item for item in runtimes)
        or len(runtimes) != len(set(runtimes))
    ):
        raise PlannerError("environment runtime_ids must be a nonempty unique string list")
    return EnvironmentBinding(
        environment_id=environment_id,
        node_class=node_class,  # type: ignore[arg-type]
        os=os_name,  # type: ignore[arg-type]
        architecture=_text(row["architecture"], "environment architecture"),
        image_sha256=_sha256(row["image_sha256"], "environment image_sha256"),
        runtime_ids=tuple(runtimes),
    )


def _parse_output(value: Any, *, gate: str, component_id: str) -> PlannedOutput:
    row = _exact_keys(
        value, {"output_path", "gate", "component_id", "artifact_sha256"}, "required output"
    )
    output = PlannedOutput(
        output_path=_text(row["output_path"], "output_path"),
        gate=_text(row["gate"], "output gate"),
        component_id=_text(row["component_id"], "output component_id"),
        artifact_sha256=_sha256(row["artifact_sha256"], "output artifact_sha256"),
    )
    if len(output.output_path) > 512 or any(
        not _OUTPUT_SEGMENT.fullmatch(segment)
        or segment.endswith(".")
        or _RESERVED_WINDOWS.fullmatch(segment)
        for segment in output.output_path.split("/")
    ):
        raise PlannerError("required output path is outside the portable relative-path dialect")
    if output.gate != gate or output.component_id != component_id:
        raise PlannerError("required output scope differs from its workload scope")
    return output


def _validate_output_set(outputs: tuple[PlannedOutput, ...]) -> None:
    if len(outputs) > _MAX_WORKLOAD_OUTPUTS:
        raise PlannerError(
            f"workload required_outputs may contain at most {_MAX_WORKLOAD_OUTPUTS} entries"
        )
    seen: set[str] = set()
    for output in outputs:
        normalized = output.output_path.casefold()
        parts = normalized.split("/")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if (
            normalized in seen
            or parents & seen
            or any(existing.startswith(normalized + "/") for existing in seen)
        ):
            raise PlannerError(
                "required outputs contain a path collision or file/directory prefix conflict"
            )
        seen.add(normalized)


def _parse_predecessor_material(value: Any) -> bool:
    if value is None:
        return False
    row = _exact_keys(
        value, {"predecessor_release_id", "material_digest"}, "predecessor material binding"
    )
    _text(row["predecessor_release_id"], "predecessor_release_id")
    _sha256(row["material_digest"], "predecessor material_digest")
    return True


def _parse_policy_binding(value: Any, label: str) -> PolicyBinding:
    row = _exact_keys(value, {"policy_id", "sha256"}, label)
    return PolicyBinding(
        policy_id=_text(row["policy_id"], f"{label} policy_id"),
        sha256=_sha256(row["sha256"], f"{label} sha256"),
    )


def _parse_input_materials(value: Any) -> tuple[MaterialBinding, ...]:
    if not isinstance(value, list) or not value:
        raise PlannerError("workload input_materials must be a nonempty array")
    result: list[MaterialBinding] = []
    seen: set[str] = set()
    for item in value:
        row = _exact_keys(item, {"material_id", "sha256"}, "input material binding")
        material_id = _text(row["material_id"], "input material_id")
        if material_id in seen:
            raise PlannerError("duplicate input material_id")
        seen.add(material_id)
        result.append(MaterialBinding(material_id, _sha256(row["sha256"], "input material sha256")))
    return tuple(sorted(result, key=lambda item: item.material_id))


def plan_fixed_workloads(
    candidate: Mapping[str, Any],
    profile: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> AdmissionPlan:
    """Build a deterministic, non-authorizing plan from detached input snapshots."""
    candidate_snapshot, candidate_bytes = _snapshot(candidate, "candidate")
    profile_snapshot, profile_bytes = _snapshot(profile, "profile")
    inventory_snapshot, inventory_bytes = _snapshot(inventory, "fixed workload inventory")

    candidate_id = _identity(candidate_snapshot, "candidate_id", "candidate")
    profile_id = _identity(profile_snapshot, "profile_id", "profile")
    inventory_row = _exact_keys(
        inventory_snapshot,
        {
            "schema_version",
            "inventory_id",
            "environments",
            "resource_policies",
            "network_policies",
            "workloads",
        },
        "fixed workload inventory",
    )
    if type(inventory_row["schema_version"]) is not int or inventory_row["schema_version"] != 1:
        raise PlannerError("fixed workload inventory schema_version must be 1")
    inventory_id = _identity(inventory_row, "inventory_id", "fixed workload inventory")

    def policy_set(field: str) -> dict[str, PolicyBinding]:
        raw = inventory_row[field]
        if not isinstance(raw, list) or not raw:
            raise PlannerError(f"{field} must be a nonempty array")
        bindings = [_parse_policy_binding(item, field) for item in raw]
        result = {binding.policy_id: binding for binding in bindings}
        if len(result) != len(bindings):
            raise PlannerError(f"duplicate policy_id in {field}")
        return result

    resource_policies = policy_set("resource_policies")
    network_policies = policy_set("network_policies")

    raw_environments = inventory_row["environments"]
    raw_workloads = inventory_row["workloads"]
    if not isinstance(raw_environments, list) or not isinstance(raw_workloads, list):
        raise PlannerError("inventory environments and workloads must be arrays")
    environments = tuple(_parse_environment(row) for row in raw_environments)
    env_by_id = {row.environment_id: row for row in environments}
    if len(env_by_id) != len(environments):
        raise PlannerError("duplicate environment_id")
    classes = [row.node_class for row in environments]
    if len(classes) != len(set(classes)):
        raise PlannerError("duplicate environment node_class")

    acceptance_rows = profile_snapshot.get("acceptance_requirements")
    target_environments = profile_snapshot.get("target_environments")
    if not isinstance(acceptance_rows, list) or not isinstance(target_environments, list):
        raise PlannerError("profile must contain acceptance_requirements and target_environments")
    if len(target_environments) != 1 or not isinstance(target_environments[0], str):
        raise PlannerError("manifest v2 requires exactly one build/deployment target")
    acceptance_scopes: set[tuple[str, str, str]] = set()
    for row in acceptance_rows:
        if not isinstance(row, dict) or set(row) != {"gate", "component_id", "environment"}:
            raise PlannerError("profile acceptance requirement is malformed")
        scope = (
            _text(row["gate"], "acceptance gate"),
            _text(row["component_id"], "acceptance component"),
            _text(row["environment"], "acceptance environment"),
        )
        if scope in acceptance_scopes:
            raise PlannerError("duplicate acceptance scope")
        if scope[0] in _DARWIN_ACCEPTANCE_GATES:
            acceptance_environment = env_by_id.get(scope[2])
            if (
                acceptance_environment is None
                or acceptance_environment.node_class != "acceptance_darwin"
            ):
                raise PlannerError(
                    f"{scope[0]} acceptance scope must use an acceptance_darwin environment"
                )
        acceptance_scopes.add(scope)

    runtimes = profile_snapshot.get("python_runtimes", [])
    if not isinstance(runtimes, list):
        raise PlannerError("profile python_runtimes must be an array")
    runtime_ids = {
        row.get("id")
        for row in runtimes
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    artifacts = candidate_snapshot.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise PlannerError("candidate artifacts must be an array")
    artifact_scopes: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if isinstance(artifact, dict) and isinstance(artifact.get("component"), str):
            file_ref = artifact.get("file")
            if isinstance(file_ref, dict) and isinstance(file_ref.get("sha256"), str):
                artifact_scopes.add((artifact["component"], file_ref["sha256"]))

    workload_plans: list[WorkloadPlan] = []
    workload_ids: set[str] = set()
    scopes: set[tuple[str, str, str, str | None]] = set()
    for raw in raw_workloads:
        row = _exact_keys(
            raw,
            {
                "workload_id",
                "workload_kind",
                "gate",
                "component_id",
                "node_class",
                "environment_id",
                "runtime_id",
                "required_outputs",
                "predecessor_material",
                "input_materials",
                "resource_policy",
                "network_policy",
            },
            "workload",
        )
        workload_id = _text(row["workload_id"], "workload_id")
        if workload_id in workload_ids:
            raise PlannerError("duplicate workload_id")
        workload_ids.add(workload_id)
        kind = _text(row["workload_kind"], "workload_kind")
        if kind not in {"gate", "compatibility", "migration", "historical_backtest"}:
            raise PlannerError("workload_kind is unsupported")
        gate = _text(row["gate"], "workload gate")
        matching_reserved_kind = next(
            (
                expected
                for prefix, expected in _RESERVED_GATE_KINDS.items()
                if gate.startswith(prefix)
            ),
            None,
        )
        component_id = _text(row["component_id"], "workload component_id")
        node_class = _text(row["node_class"], "workload node_class")
        if node_class not in NODE_CLASSES:
            raise PlannerError("workload node_class is unsupported")
        environment_id = _text(row["environment_id"], "workload environment_id")
        runtime_id = _text(row["runtime_id"], "runtime_id")
        resource_policy = _parse_policy_binding(row["resource_policy"], "workload resource_policy")
        network_policy = _parse_policy_binding(row["network_policy"], "workload network_policy")
        raw_outputs = row["required_outputs"]
        if not isinstance(raw_outputs, list) or not raw_outputs:
            raise PlannerError("workload required_outputs must be a nonempty array")
        outputs = tuple(
            _parse_output(output, gate=gate, component_id=component_id) for output in raw_outputs
        )
        _validate_output_set(outputs)
        scope = (gate, component_id, environment_id, runtime_id)
        if scope in scopes:
            raise PlannerError("duplicate workload evidence scope")
        scopes.add(scope)

        workload_blockers: list[str] = []
        if resource_policies.get(resource_policy.policy_id) != resource_policy:
            workload_blockers.append("resource_policy_binding_unknown_or_mismatched")
        if network_policies.get(network_policy.policy_id) != network_policy:
            workload_blockers.append("network_policy_binding_unknown_or_mismatched")
        if matching_reserved_kind is not None and kind != matching_reserved_kind:
            workload_blockers.append(f"reserved_gate_kind_mismatch:{matching_reserved_kind}")
        if kind in set(_RESERVED_GATE_KINDS.values()) and matching_reserved_kind != kind:
            workload_blockers.append(f"workload_kind_requires_gate_namespace:{kind}")
        environment = env_by_id.get(environment_id)
        if gate in _DARWIN_ACCEPTANCE_GATES and (
            node_class != "acceptance_darwin"
            or environment is None
            or environment.node_class != "acceptance_darwin"
        ):
            raise PlannerError(
                f"{gate} workload and outputs must use an acceptance_darwin environment"
            )
        if environment is None:
            workload_blockers.append("environment_binding_missing")
        elif environment.node_class != node_class:
            workload_blockers.append("environment_node_class_mismatch")
        elif node_class == "paper_linux" and environment_id not in target_environments:
            workload_blockers.append("paper_environment_not_manifest_target")
        elif (gate, component_id, environment_id) not in acceptance_scopes:
            workload_blockers.append("acceptance_evidence_scope_missing")
        if runtime_id not in runtime_ids:
            workload_blockers.append("runtime_not_declared_by_profile")
        if environment is not None and runtime_id not in environment.runtime_ids:
            workload_blockers.append("runtime_not_bound_to_environment")

        for output in outputs:
            if (output.gate, output.component_id, environment_id) not in acceptance_scopes:
                workload_blockers.append("required_evidence_scope_missing")
            if (output.component_id, output.artifact_sha256) not in artifact_scopes:
                workload_blockers.append("required_evidence_artifact_missing")
        if kind in {"compatibility", "migration"} and not _parse_predecessor_material(
            row["predecessor_material"]
        ):
            workload_blockers.append("selected_predecessor_material_binding_missing")
        elif row["predecessor_material"] is not None:
            _parse_predecessor_material(row["predecessor_material"])
        if kind == "historical_backtest" and node_class != "historical_backtester":
            workload_blockers.append("historical_backtest_node_class_required")
        if node_class == "historical_backtester" and kind != "historical_backtest":
            workload_blockers.append("backtester_environment_scope_mismatch")
        if node_class == "acceptance_darwin" and kind == "historical_backtest":
            workload_blockers.append("darwin_environment_cannot_substitute_for_backtester")
        if node_class == "historical_backtester" and environment_id in target_environments:
            workload_blockers.append("backtester_environment_cannot_substitute_for_deployment")
        if node_class == "acceptance_darwin" and environment_id in target_environments:
            workload_blockers.append("darwin_environment_cannot_substitute_for_deployment")

        workload_sha256 = _digest(_canonical_bytes(row, "workload"))
        input_materials = _parse_input_materials(row["input_materials"])
        workload_plans.append(
            WorkloadPlan(
                workload_id=workload_id,
                workload_kind=kind,
                gate=gate,
                component_id=component_id,
                node_class=node_class,
                environment_id=environment_id,
                runtime_id=runtime_id,
                workload_sha256=workload_sha256,
                input_materials=input_materials,
                resource_policy=resource_policy,
                network_policy=network_policy,
                required_outputs=outputs,
                blockers=tuple(sorted(set(workload_blockers))),
            )
        )

    aggregate_blockers: list[str] = []
    planned_evidence_scopes = {
        (workload.gate, workload.component_id, workload.environment_id)
        for workload in workload_plans
    }
    for gate, component_id, environment_id in sorted(acceptance_scopes - planned_evidence_scopes):
        aggregate_blockers.append(
            f"profile_acceptance_scope_without_workload:{gate}:{component_id}:{environment_id}"
        )
    workload_classes = {workload.node_class for workload in workload_plans}
    for required_class in sorted(NODE_CLASSES):
        if required_class not in classes:
            aggregate_blockers.append(f"environment_node_class_missing:{required_class}")
        if required_class not in workload_classes:
            aggregate_blockers.append(f"workload_node_class_missing:{required_class}")
    for workload in workload_plans:
        aggregate_blockers.extend(
            f"{workload.workload_id}:{blocker}" for blocker in workload.blockers
        )
    return AdmissionPlan(
        candidate_id=candidate_id,
        profile_id=profile_id,
        candidate_snapshot_sha256=_digest(candidate_bytes),
        profile_snapshot_sha256=_digest(profile_bytes),
        inventory_id=inventory_id,
        inventory_snapshot_sha256=_digest(inventory_bytes),
        workloads=tuple(sorted(workload_plans, key=lambda item: item.workload_id)),
        blockers=tuple(sorted(set(aggregate_blockers))),
    )
