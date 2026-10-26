from __future__ import annotations

import copy
import hashlib
import io
import json
import unicodedata
import zipfile
from dataclasses import replace
from typing import Any

import pytest

from scripts import compatibility_material_set as material_set
from scripts.external_admission_ledger import (
    Admission,
    NetworkPolicy,
    ResourcePolicy,
    admission_digest,
)
from scripts.platform_release_manifest import (
    canonical_json_bytes,
    compatibility_subject_sha256,
    object_identity,
)

_FORMAT_ID = "urn:fixture:configuration:v1"
_SOURCE_SHA = "a" * 40
_ENVIRONMENT = "fixture-linux"
_RUNTIME = "cpython-3.12.8"


def _wheel(component_id: str, version: str, contract: bytes, member_path: str) -> bytes:
    name = component_id.replace("-", "_")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.2\nName: {component_id}\nVersion: {version}\n\n",
        )
        archive.writestr(member_path, contract)
    return output.getvalue()


def _ref(path: str, content: bytes) -> dict[str, str]:
    return {"origin": "bundle", "path": path, "sha256": hashlib.sha256(content).hexdigest()}


def _schema(title: str = "fixture") -> bytes:
    return canonical_json_bytes(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": _FORMAT_ID,
            "title": title,
            "type": "object",
            "properties": {"naïve": {"type": "string"}},
        }
    )


def _candidate(version: str, wheel: bytes, path: str) -> dict[str, Any]:
    component_id = "fixture-package"
    component = {
        "id": component_id,
        "kind": "python_distribution",
        "version": version,
        "descriptor": {"origin": "source", "path": "pyproject.toml", "sha256": "b" * 64},
        "source_files": [{"origin": "source", "path": "pyproject.toml", "sha256": "b" * 64}],
    }
    reference = _ref(path, wheel)
    candidate: dict[str, Any] = {
        "candidate_id": "0" * 64,
        "source": {"commit": _SOURCE_SHA},
        "components": [component],
        "artifacts": [{"component": component_id, "kind": "distribution", "file": reference}],
        "compatibility_coverage": [],
        "migrations": [],
        "rollback": [],
    }
    candidate["candidate_id"] = object_identity(candidate, "candidate_id")
    return candidate


def _profile() -> dict[str, Any]:
    profile: dict[str, Any] = {
        "profile_id": "0" * 64,
        "python_runtimes": [{"id": _RUNTIME}],
        "acceptance_requirements": [
            {
                "gate": "compatibility:provider-fixture",
                "component_id": "fixture-package",
                "environment": _ENVIRONMENT,
            }
        ],
    }
    profile["profile_id"] = object_identity(profile, "profile_id")
    return profile


def _case() -> dict[str, Any]:
    pred_contract = _schema("predecessor")
    target_contract = _schema("target")
    pred_member, target_member = "schemas/naïve.json", "schemas/naïve.json"
    pred_wheel = _wheel("fixture-package", "1.0", pred_contract, pred_member)
    target_wheel = _wheel("fixture-package", "2.0", target_contract, target_member)
    predecessor = _candidate("1.0", pred_wheel, "dist/fixture_package-1.0-py3-none-any.whl")
    target = _candidate("2.0", target_wheel, "dist/fixture_package-2.0-py3-none-any.whl")
    pred_profile, target_profile = _profile(), _profile()
    scope = {
        "environment_id": _ENVIRONMENT,
        "runtime_id": _RUNTIME,
        "runtime_status": "declared",
    }
    predecessor["compatibility_coverage"] = [
        {
            "component_id": "fixture-package",
            "status": "confirmed",
            "owner": "fixture owner",
            "formats": [
                {
                    "format_id": _FORMAT_ID,
                    "format_kind": "json_schema",
                    "producers": ["fixture-package"],
                    "consumers": ["fixture-package"],
                    "relations": [],
                }
            ],
        }
    ]
    predecessor["candidate_id"] = object_identity(predecessor, "candidate_id")
    relation: dict[str, Any] = {
        "scope": scope,
        "predecessor": {
            "release_id": "fixture-release-1",
            "candidate_id": predecessor["candidate_id"],
            "source_commit": _SOURCE_SHA,
            "format_id": _FORMAT_ID,
        },
        "target": {
            "source_commit": _SOURCE_SHA,
            "component_id": "fixture-package",
            "component_version": "2.0",
            "descriptor_sha256": "b" * 64,
            "format_id": _FORMAT_ID,
        },
        "disposition": "migrate",
        "migration_id": "upgrade-config",
        "rollback_id": "rollback-config",
    }
    row = {
        "component_id": "fixture-package",
        "status": "confirmed",
        "inventory_status": "enumerated",
        "owner": "fixture owner",
        "formats": [
            {
                "format_id": _FORMAT_ID,
                "format_kind": "json_schema",
                "producers": ["fixture-package"],
                "consumers": ["fixture-package"],
                "relations": [relation],
            }
        ],
    }
    target["compatibility_coverage"] = [row]
    target["migrations"] = [
        {
            "id": "upgrade-config",
            "from": "fixture-release-1",
            "to": "2.0",
            "evidence_required": True,
        }
    ]
    target["rollback"] = [
        {
            "id": "rollback-config",
            "from_candidate": "self",
            "to_candidate": predecessor["candidate_id"],
            "evidence_required": True,
        }
    ]
    target["candidate_id"] = object_identity(target, "candidate_id")
    subject = compatibility_subject_sha256(
        {
            "kind": "compatibility_relation",
            "component_id": "fixture-package",
            "owner": "fixture owner",
            "format_id": _FORMAT_ID,
            "relation": relation,
            "scope": scope,
        }
    )
    predecessor_manifest = {
        "candidate": predecessor,
        "support_profile": pred_profile,
        "release_decision": {
            "decision": "accepted",
            "candidate_id": predecessor["candidate_id"],
            "profile_id": pred_profile["profile_id"],
        },
    }
    pred_manifest_bytes = json.dumps(
        predecessor_manifest, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    roles = {
        "predecessor_manifest": {"sha256": hashlib.sha256(pred_manifest_bytes).hexdigest()},
        "predecessor_artifact": {
            "component_id": "fixture-package",
            "file": predecessor["artifacts"][0]["file"],
        },
        "predecessor_contract": {
            "sha256": hashlib.sha256(pred_contract).hexdigest(),
            "member_path": pred_member,
        },
        "target_artifact": {"file": target["artifacts"][0]["file"]},
        "target_contract": {
            "sha256": hashlib.sha256(target_contract).hexdigest(),
            "member_path": target_member,
        },
    }
    descriptor = {
        "contract_type": material_set.CONTRACT_TYPE,
        "contract_version": material_set.CONTRACT_VERSION,
        "compatibility_subject_sha256": subject,
        "materials": roles,
    }
    descriptor_bytes = canonical_json_bytes(descriptor)
    material_digest = hashlib.sha256(descriptor_bytes).hexdigest()
    workload_spec = {**material_set.WORKLOAD_SPEC_BASE, "implementation_sha256": "e" * 64}
    workload_digest = material_set.workload_spec_digest(workload_spec)
    admission = Admission(
        admission_id="00000000-0000-4000-8000-000000000001",
        grant_digest="0" * 64,
        repository="https://github.com/MishkaStrategy/Module-Strategy.git",
        source_sha=_SOURCE_SHA,
        workflow_digest="1" * 64,
        runtime_id=_RUNTIME,
        workload_id=material_set.WORKLOAD_ID,
        workload_digest=workload_digest,
        policy_id="fixture-policy",
        policy_digest="2" * 64,
        environment_id=_ENVIRONMENT,
        image_digest="3" * 64,
        image_os="linux",
        image_arch="x86_64",
        provider_id="fixture-provider",
        provider_run_id="fixture-run",
        provider_attempt=1,
        candidate_id=target["candidate_id"],
        candidate_digest=hashlib.sha256(canonical_json_bytes(target)).hexdigest(),
        profile_id=target_profile["profile_id"],
        profile_digest=hashlib.sha256(canonical_json_bytes(target_profile)).hexdigest(),
        material_id=material_set.MATERIAL_ID,
        material_digest=material_digest,
        required_output_paths=(),
        resource_policy=ResourcePolicy(1000, 1024 * 1024, 1024 * 1024, 2, 60),
        network_policy=NetworkPolicy("disabled", ()),
        issued_at="2026-01-01T00:00:00Z",
        expires_at="2026-01-01T00:05:00Z",
        execution_deadline="2026-01-01T00:06:00Z",
        publication_deadline="2026-01-01T00:07:00Z",
    )
    receipt = {
        "admission_id": admission.admission_id,
        "admission_digest": admission_digest(admission),
        "source_sha": admission.source_sha,
        "runtime_id": _RUNTIME,
        "workload_id": material_set.WORKLOAD_ID,
        "workload_digest": workload_digest,
        "material_digest": material_digest,
        "environment_id": _ENVIRONMENT,
    }
    return {
        "descriptor_bytes": descriptor_bytes,
        "predecessor_manifest_bytes": pred_manifest_bytes,
        "predecessor_artifact_bytes": pred_wheel,
        "target_candidate": target,
        "target_profile": target_profile,
        "target_artifact_bytes": target_wheel,
        "admission": admission,
        "receipt_payload": receipt,
        "workload_spec": copy.deepcopy(workload_spec),
        "descriptor": descriptor,
        "predecessor": predecessor,
    }


def _validate(case: dict[str, Any]) -> material_set.MaterialSetResult:
    return material_set.validate_compatibility_material_set(
        descriptor_bytes=case["descriptor_bytes"],
        predecessor_manifest_bytes=case["predecessor_manifest_bytes"],
        predecessor_artifact_bytes=case["predecessor_artifact_bytes"],
        target_candidate=case["target_candidate"],
        target_profile=case["target_profile"],
        target_artifact_bytes=case["target_artifact_bytes"],
        admission=case["admission"],
        receipt_payload=case["receipt_payload"],
        workload_spec=case["workload_spec"],
    )


def _reencode_descriptor(case: dict[str, Any]) -> None:
    case["descriptor"]["materials"]["predecessor_manifest"]["sha256"] = hashlib.sha256(
        case["predecessor_manifest_bytes"]
    ).hexdigest()
    case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])
    new_digest = hashlib.sha256(case["descriptor_bytes"]).hexdigest()
    case["admission"] = replace(case["admission"], material_digest=new_digest)
    case["receipt_payload"]["material_digest"] = new_digest
    case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])


def _rebind_target_candidate(case: dict[str, Any]) -> None:
    candidate = case["target_candidate"]
    candidate["candidate_id"] = object_identity(candidate, "candidate_id")
    row = candidate["compatibility_coverage"][0]
    fmt = row["formats"][0]
    relation = fmt["relations"][0]
    case["descriptor"]["compatibility_subject_sha256"] = compatibility_subject_sha256(
        {
            "kind": "compatibility_relation",
            "component_id": row["component_id"],
            "owner": row["owner"],
            "format_id": fmt["format_id"],
            "relation": relation,
            "scope": relation["scope"],
        }
    )
    case["admission"] = replace(
        case["admission"],
        candidate_id=candidate["candidate_id"],
        candidate_digest=hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
    )
    case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    _reencode_descriptor(case)


@pytest.mark.parametrize(
    "field,value",
    [
        ("component_id", "other-package"),
        ("component_version", "9.0"),
        ("descriptor_sha256", "f" * 64),
    ],
)
def test_material_set_rejects_target_component_relation_mismatch(field: str, value: str) -> None:
    case = _case()
    relation = case["target_candidate"]["compatibility_coverage"][0]["formats"][0]["relations"][0]
    relation["target"][field] = value
    _rebind_target_candidate(case)

    with pytest.raises(material_set.MaterialSetError, match="target component/version/descriptor"):
        _validate(case)


def test_material_set_requires_predecessor_accepted_claim() -> None:
    case = _case()
    manifest = json.loads(case["predecessor_manifest_bytes"])
    manifest["release_decision"]["decision"] = "rejected"
    case["predecessor_manifest_bytes"] = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    _reencode_descriptor(case)

    with pytest.raises(material_set.MaterialSetError, match="does not claim accepted"):
        _validate(case)


def test_material_set_hashes_canonical_unicode_descriptor_and_binds_receipt() -> None:
    case = _case()
    result = _validate(case)
    assert result.material_digest == hashlib.sha256(case["descriptor_bytes"]).hexdigest()
    assert result.workload_digest == material_set.workload_spec_digest(case["workload_spec"])
    assert _validate(case) == result


@pytest.mark.parametrize("bad_digest", ["A" * 64, "a" * 63, "g" * 64, 123])
def test_material_set_rejects_malformed_contract_digest_values(bad_digest: Any) -> None:
    case = _case()
    case["descriptor"]["materials"]["target_contract"]["sha256"] = bad_digest
    case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])

    with pytest.raises(material_set.MaterialSetError, match="lowercase SHA-256"):
        _validate(case)


def test_material_set_rejects_stale_admission_digest_for_valid_descriptor_change() -> None:
    case = _case()
    case["descriptor"]["materials"]["target_contract"]["member_path"] = (
        "schemas/other-contract.json"
    )
    case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])

    with pytest.raises(material_set.MaterialSetError, match="material identity/digest"):
        _validate(case)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_role",
        "missing_role",
        "wrong_predecessor_component",
        "predecessor_target_role_swap",
        "target_predecessor_role_swap",
        "wrong_predecessor_artifact_digest",
        "wrong_contract_digest",
        "wrong_predecessor_candidate",
        "wrong_scope",
        "wrong_subject",
        "disposition_tamper",
        "migration_tamper",
        "rollback_tamper",
        "workload_mismatch",
        "implementation_digest_mismatch",
        "target_candidate_binding_mismatch",
        "admission_source_mismatch",
        "admission_scope_mismatch",
        "receipt_binding_mismatch",
        "receipt_source_mismatch",
        "wrong_target_artifact_bytes",
    ],
)
def test_material_set_rejects_role_and_binding_tampering(mutation: str) -> None:
    case = _case()
    descriptor = case["descriptor"]
    roles = descriptor["materials"]
    if mutation == "extra_role":
        roles["unexpected"] = {}
    elif mutation == "missing_role":
        roles.pop("target_contract")
    elif mutation == "wrong_predecessor_component":
        roles["predecessor_artifact"]["component_id"] = "other-package"
        _reencode_descriptor(case)
    elif mutation == "predecessor_target_role_swap":
        roles["predecessor_artifact"]["file"], roles["target_artifact"]["file"] = (
            roles["target_artifact"]["file"],
            roles["predecessor_artifact"]["file"],
        )
        _reencode_descriptor(case)
    elif mutation == "target_predecessor_role_swap":
        roles["predecessor_contract"], roles["target_contract"] = (
            roles["target_contract"],
            roles["predecessor_contract"],
        )
        _reencode_descriptor(case)
    elif mutation == "wrong_predecessor_artifact_digest":
        roles["predecessor_artifact"]["file"]["sha256"] = "f" * 64
        _reencode_descriptor(case)
    elif mutation == "wrong_contract_digest":
        roles["target_contract"]["sha256"] = "f" * 64
        _reencode_descriptor(case)
    elif mutation == "wrong_predecessor_candidate":
        case["predecessor"]["source"]["other_commit_claim"] = "z"
        case["predecessor"]["candidate_id"] = object_identity(case["predecessor"], "candidate_id")
        manifest = json.loads(case["predecessor_manifest_bytes"])
        manifest["candidate"] = case["predecessor"]
        manifest["release_decision"]["candidate_id"] = case["predecessor"]["candidate_id"]
        case["predecessor_manifest_bytes"] = json.dumps(manifest, separators=(",", ":")).encode()
        roles["predecessor_manifest"]["sha256"] = hashlib.sha256(
            case["predecessor_manifest_bytes"]
        ).hexdigest()
        _reencode_descriptor(case)
    elif mutation == "wrong_scope":
        relation = case["target_candidate"]["compatibility_coverage"][0]["formats"][0]["relations"][
            0
        ]
        relation["scope"]["environment_id"] = "other-environment"
        case["target_candidate"]["candidate_id"] = object_identity(
            case["target_candidate"], "candidate_id"
        )
        case["admission"] = replace(
            case["admission"],
            candidate_id=case["target_candidate"]["candidate_id"],
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(case["target_candidate"])
            ).hexdigest(),
        )
        case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    elif mutation in {"wrong_subject", "disposition_tamper", "migration_tamper", "rollback_tamper"}:
        relation = case["target_candidate"]["compatibility_coverage"][0]["formats"][0]["relations"][
            0
        ]
        if mutation == "disposition_tamper":
            relation["disposition"] = "unchanged-compatible"
        elif mutation == "migration_tamper":
            relation["migration_id"] = "other-migration"
        elif mutation == "rollback_tamper":
            relation["rollback_id"] = "other-rollback"
        else:
            descriptor["compatibility_subject_sha256"] = "f" * 64
        if mutation != "wrong_subject":
            case["target_candidate"]["candidate_id"] = object_identity(
                case["target_candidate"], "candidate_id"
            )
            case["admission"] = replace(
                case["admission"],
                candidate_id=case["target_candidate"]["candidate_id"],
                candidate_digest=hashlib.sha256(
                    canonical_json_bytes(case["target_candidate"])
                ).hexdigest(),
            )
            case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    elif mutation == "workload_mismatch":
        case["workload_spec"]["operation"] = "different_operation"
    elif mutation == "implementation_digest_mismatch":
        case["workload_spec"]["implementation_sha256"] = "f" * 64
    elif mutation == "target_candidate_binding_mismatch":
        case["admission"] = replace(case["admission"], candidate_id="f" * 64)
        case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    elif mutation == "admission_source_mismatch":
        case["admission"] = replace(case["admission"], source_sha="9" * 40)
        case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    elif mutation == "admission_scope_mismatch":
        case["admission"] = replace(case["admission"], environment_id="other-environment")
        case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
        case["receipt_payload"]["environment_id"] = "other-environment"
    elif mutation == "receipt_binding_mismatch":
        case["receipt_payload"]["workload_digest"] = "f" * 64
    elif mutation == "receipt_source_mismatch":
        case["receipt_payload"]["source_sha"] = "9" * 40
    elif mutation == "wrong_target_artifact_bytes":
        case["target_artifact_bytes"] += b"tampered"
    if mutation in {"extra_role", "missing_role"}:
        case["descriptor_bytes"] = canonical_json_bytes(descriptor)
    elif mutation == "wrong_subject":
        case["descriptor_bytes"] = canonical_json_bytes(descriptor)
        new_digest = hashlib.sha256(case["descriptor_bytes"]).hexdigest()
        case["admission"] = replace(case["admission"], material_digest=new_digest)
        case["receipt_payload"]["material_digest"] = new_digest
    with pytest.raises(material_set.MaterialSetError):
        _validate(case)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"contract_type":"compatibility_material_set","contract_type":"compatibility_material_set"}',
        b'{"bad":NaN}',
        b"not-json",
    ],
)
def test_material_set_rejects_duplicate_or_malformed_descriptor_json(raw: bytes) -> None:
    case = _case()
    case["descriptor_bytes"] = raw
    with pytest.raises(material_set.MaterialSetError):
        _validate(case)


def test_material_set_rejects_duplicate_json_keys_in_predecessor_manifest() -> None:
    case = _case()
    case["predecessor_manifest_bytes"] = b'{"candidate":{},"candidate":{}}'
    case["descriptor"]["materials"]["predecessor_manifest"]["sha256"] = hashlib.sha256(
        case["predecessor_manifest_bytes"]
    ).hexdigest()
    case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])
    digest = hashlib.sha256(case["descriptor_bytes"]).hexdigest()
    case["admission"] = replace(case["admission"], material_digest=digest)
    case["receipt_payload"]["material_digest"] = digest

    with pytest.raises(material_set.MaterialSetError, match="strict JSON"):
        _validate(case)


@pytest.mark.parametrize(
    "archive_case", ["duplicate", "archive_traversal", "traversal", "case_collision", "ambiguous"]
)
def test_material_set_rejects_noncanonical_or_ambiguous_wheel_selector(archive_case: str) -> None:
    case = _case()
    contract = _schema()
    member = case["descriptor"]["materials"]["target_contract"]["member_path"]
    extras = []
    if archive_case == "duplicate":
        extras = [member]
    elif archive_case == "archive_traversal":
        extras = ["schemas/../escape.json"]
    elif archive_case == "traversal":
        case["descriptor"]["materials"]["target_contract"]["member_path"] = "schemas/../config.json"
        case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])
        digest = hashlib.sha256(case["descriptor_bytes"]).hexdigest()
        case["admission"] = replace(case["admission"], material_digest=digest)
        case["receipt_payload"]["material_digest"] = digest
    elif archive_case == "case_collision":
        extras = ["schemas/NAÏVE.json"]
    else:
        extras = [unicodedata.normalize("NFD", member)]
    if extras:
        wheel = _wheel("fixture-package", "2.0", contract, member)
        original = zipfile.ZipFile(io.BytesIO(wheel))
        output = io.BytesIO()
        with original, zipfile.ZipFile(output, "w") as rewritten:
            for info in original.infolist():
                rewritten.writestr(info, original.read(info))
            for index, path in enumerate(extras):
                rewritten.writestr(path, contract if index == 0 else b"{}")
        wheel = output.getvalue()
        path = "dist/fixture_package-2.0-py3-none-any.whl"
        case["target_artifact_bytes"] = wheel
        case["target_candidate"]["artifacts"][0]["file"] = _ref(path, wheel)
        case["descriptor"]["materials"]["target_artifact"]["file"] = _ref(path, wheel)
        case["descriptor"]["materials"]["target_contract"]["sha256"] = hashlib.sha256(
            contract
        ).hexdigest()
        case["descriptor_bytes"] = canonical_json_bytes(case["descriptor"])
        digest = hashlib.sha256(case["descriptor_bytes"]).hexdigest()
        case["admission"] = replace(
            case["admission"],
            material_digest=digest,
            candidate_digest=hashlib.sha256(
                canonical_json_bytes(case["target_candidate"])
            ).hexdigest(),
        )
        case["receipt_payload"]["material_digest"] = digest
        case["receipt_payload"]["admission_digest"] = admission_digest(case["admission"])
    with pytest.raises(material_set.MaterialSetError):
        _validate(case)


def test_material_set_is_pure_and_does_not_unlock_release_acceptance() -> None:
    case = _case()
    before = copy.deepcopy(case["target_candidate"])
    result = _validate(case)
    assert not hasattr(result, "decision")
    assert case["target_candidate"] == before
    assert "release_decision" not in case["target_candidate"]
