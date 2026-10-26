"""Pure draft validation for a candidate-external compatibility material set.

This module checks caller-supplied immutable byte snapshots and existing
admission/receipt bindings. It does not resolve a registry, authenticate a
provider or receipt, or alter release acceptance decisions.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, NoReturn

from jsonschema.validators import validator_for

from scripts import platform_release_manifest as release_manifest
from scripts.external_admission_ledger import Admission, admission_digest

CONTRACT_TYPE = "compatibility_material_set"
CONTRACT_VERSION = 1
MATERIAL_ID = "compatibility-material-set-v1"
WORKLOAD_ID = "compatibility-wheel-json-schema-v1"
WORKLOAD_SPEC_BASE: dict[str, Any] = {
    "contract_type": "compatibility_material_set_workload",
    "version": 1,
    "adapter": "scripts.compatibility_material_set",
    "operation": "validate_wheel_json_schema_relation_material",
}
_WORKLOAD_SPEC_FIELDS = {*WORKLOAD_SPEC_BASE, "implementation_sha256"}
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class MaterialSetError(ValueError):
    """A draft compatibility material set is malformed or incorrectly bound."""


@dataclass(frozen=True)
class MaterialSetResult:
    relation_subject_sha256: str
    material_digest: str
    workload_id: str
    workload_digest: str


def workload_spec_digest(workload_spec: Mapping[str, Any]) -> str:
    """Hash the closed workload spec with an externally supplied implementation digest."""
    if not isinstance(workload_spec, Mapping) or set(workload_spec) != _WORKLOAD_SPEC_FIELDS:
        _fail("workload", f"spec must contain exactly {sorted(_WORKLOAD_SPEC_FIELDS)}")
    if any(workload_spec.get(key) != value for key, value in WORKLOAD_SPEC_BASE.items()):
        _fail("workload", "adapter/operation workload spec is unsupported")
    implementation_sha256 = _check_digest(
        workload_spec.get("implementation_sha256"), "workload.implementation_sha256"
    )
    frozen = {**WORKLOAD_SPEC_BASE, "implementation_sha256": implementation_sha256}
    return hashlib.sha256(release_manifest.canonical_json_bytes(frozen)).hexdigest()


def _fail(label: str, detail: str) -> NoReturn:
    raise MaterialSetError(f"{label}: {detail}")


def _strict_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=release_manifest._pairs_no_duplicates,
            parse_constant=release_manifest._reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, release_manifest.ManifestError) as exc:
        raise MaterialSetError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        _fail(label, "must be a JSON object")
    return value


def _exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        _fail(label, f"must contain exactly {sorted(required)}")
    return value


def _check_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        _fail(label, "must be a lowercase SHA-256 digest")
    return value


def _canonical_descriptor(raw: bytes) -> tuple[dict[str, Any], str]:
    descriptor = _strict_object(raw, "material-set descriptor")
    _exact_keys(
        descriptor,
        {"contract_type", "contract_version", "compatibility_subject_sha256", "materials"},
        "material-set descriptor",
    )
    if (
        descriptor["contract_type"] != CONTRACT_TYPE
        or descriptor["contract_version"] != CONTRACT_VERSION
    ):
        _fail("material-set descriptor", "unsupported contract type or version")
    _check_digest(descriptor["compatibility_subject_sha256"], "compatibility_subject_sha256")
    if release_manifest.canonical_json_bytes(descriptor) != raw:
        _fail("material-set descriptor", "bytes are not canonical JSON")
    roles = _exact_keys(
        descriptor["materials"],
        {
            "predecessor_manifest",
            "predecessor_artifact",
            "predecessor_contract",
            "target_artifact",
            "target_contract",
        },
        "materials",
    )
    _exact_keys(roles["predecessor_manifest"], {"sha256"}, "predecessor_manifest role")
    _check_digest(roles["predecessor_manifest"]["sha256"], "predecessor_manifest.sha256")
    _exact_keys(
        roles["predecessor_artifact"], {"component_id", "file"}, "predecessor_artifact role"
    )
    if (
        not isinstance(roles["predecessor_artifact"]["component_id"], str)
        or not roles["predecessor_artifact"]["component_id"]
    ):
        _fail("predecessor_artifact.component_id", "must be a nonempty string")
    _file_reference(roles["predecessor_artifact"]["file"], "predecessor_artifact.file")
    _exact_keys(
        roles["predecessor_contract"], {"sha256", "member_path"}, "predecessor_contract role"
    )
    _check_digest(roles["predecessor_contract"]["sha256"], "predecessor_contract.sha256")
    _canonical_member_path(
        roles["predecessor_contract"]["member_path"], "predecessor_contract.member_path"
    )
    _exact_keys(roles["target_artifact"], {"file"}, "target_artifact role")
    _file_reference(roles["target_artifact"]["file"], "target_artifact.file")
    _exact_keys(roles["target_contract"], {"sha256", "member_path"}, "target_contract role")
    _check_digest(roles["target_contract"]["sha256"], "target_contract.sha256")
    _canonical_member_path(roles["target_contract"]["member_path"], "target_contract.member_path")
    material_digest = hashlib.sha256(raw).hexdigest()
    return descriptor, material_digest


def _file_reference(value: Any, label: str) -> dict[str, str]:
    reference = _exact_keys(value, {"origin", "path", "sha256"}, label)
    if reference["origin"] != "bundle":
        _fail(label, "must refer to a candidate bundle artifact")
    path = reference["path"]
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or PurePosixPath(path).is_absolute()
        or str(PurePosixPath(path)) != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        _fail(label, "path must be canonical and bundle-relative")
    _check_digest(reference["sha256"], f"{label}.sha256")
    return reference


def _canonical_member_path(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or str(PurePosixPath(value)) != value
        or unicodedata.normalize("NFC", value) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        _fail(label, "must be a canonical wheel member path")
    return value


def _identity(value: dict[str, Any], field: str, label: str) -> str:
    try:
        return release_manifest._check_identity(value, field, label)
    except release_manifest.ManifestError as exc:
        raise MaterialSetError(str(exc)) from exc


def _candidate_components(candidate: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    components = candidate.get("components")
    if not isinstance(components, list) or not components:
        _fail(label, "candidate components must be a nonempty array")
    output: dict[str, dict[str, Any]] = {}
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("id"), str):
            _fail(label, "candidate component entry is malformed")
        if component["id"] in output:
            _fail(label, "candidate component IDs are duplicated")
        output[component["id"]] = component
    return output


def _check_file_bytes(reference: dict[str, str], content: bytes, label: str) -> None:
    if not isinstance(content, bytes):
        _fail(label, "caller-supplied artifact bytes are required")
    if hashlib.sha256(content).hexdigest() != reference["sha256"]:
        _fail(label, "artifact SHA-256 does not match its file reference")
    if len(content) > release_manifest.MAX_DISTRIBUTION_ARCHIVE_BYTES:
        _fail(label, "wheel exceeds the bounded compressed-size limit")


def _bound_wheel(
    candidate: dict[str, Any],
    components: dict[str, dict[str, Any]],
    component_id: str,
    reference: dict[str, str],
    content: bytes,
    label: str,
) -> zipfile.ZipFile:
    component = components.get(component_id)
    if component is None or component.get("kind") != "python_distribution":
        _fail(label, "component must be a Python distribution in the frozen candidate")
    artifacts = candidate.get("artifacts")
    if not isinstance(artifacts, list):
        _fail(label, "candidate artifact inventory is malformed")
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("component") == component_id
        and artifact.get("kind") == "distribution"
        and artifact.get("file") == reference
    ]
    if len(matches) != 1:
        _fail(label, "file reference must exactly match one candidate distribution artifact")
    _check_file_bytes(reference, content, label)
    try:
        artifact_format = release_manifest._validate_python_distribution_artifact(
            reference["path"], content, component
        )
    except release_manifest.ManifestError as exc:
        raise MaterialSetError(str(exc)) from exc
    if artifact_format != "wheel":
        _fail(label, "only wheel artifacts are supported")
    try:
        return zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as exc:
        raise MaterialSetError(f"{label} is not a valid wheel") from exc


def _extract_schema_member(
    archive: zipfile.ZipFile, member_path: str, expected_sha256: str, format_id: str, label: str
) -> dict[str, Any]:
    infos = archive.infolist()
    seen_folded: set[str] = set()
    selected: list[zipfile.ZipInfo] = []
    for info in infos:
        try:
            parts = release_manifest._archive_member_parts(
                info.filename, directory=info.is_dir(), label=label
            )
        except release_manifest.ManifestError as exc:
            raise MaterialSetError(str(exc)) from exc
        if info.filename.endswith("/") or info.is_dir():
            canonical = "/".join(parts) + "/"
        else:
            canonical = "/".join(parts)
        folded = unicodedata.normalize("NFC", canonical).casefold()
        if folded in seen_folded:
            _fail(label, "wheel contains a case-insensitive archive path collision")
        seen_folded.add(folded)
        if canonical == member_path:
            selected.append(info)
    if len(selected) != 1:
        _fail(label, "wheel must contain exactly one canonical contract member")
    info = selected[0]
    if info.is_dir() or info.file_size > release_manifest.MAX_DISTRIBUTION_METADATA_BYTES:
        _fail(label, "contract member is a directory or exceeds the metadata-size limit")
    try:
        with archive.open(info) as stream:
            content = stream.read(release_manifest.MAX_DISTRIBUTION_METADATA_BYTES + 1)
    except (OSError, EOFError, zipfile.BadZipFile, RuntimeError) as exc:
        raise MaterialSetError(f"{label} contract member cannot be read") from exc
    if (
        len(content) != info.file_size
        or len(content) > release_manifest.MAX_DISTRIBUTION_METADATA_BYTES
    ):
        _fail(label, "contract member size is inconsistent or too large")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        _fail(label, "contract member SHA-256 does not match")
    contract = _strict_object(content, label)
    if not isinstance(contract.get("$id"), str) or contract["$id"] != format_id:
        _fail(label, "JSON Schema $id does not match the declared format ID")
    if not isinstance(contract.get("$schema"), str):
        _fail(label, "JSON Schema must declare $schema")
    try:
        validator_for(contract).check_schema(contract)
    except Exception as exc:
        raise MaterialSetError(f"{label} is not a valid supported JSON Schema") from exc
    return contract


def _profile_scope(
    candidate: dict[str, Any], profile: dict[str, Any], component_id: str, scope: Any
) -> dict[str, Any]:
    if not isinstance(scope, dict) or set(scope) != {
        "environment_id",
        "runtime_id",
        "runtime_status",
    }:
        _fail("relation scope", "is malformed")
    environments = {
        row.get("environment")
        for row in profile.get("acceptance_requirements", [])
        if isinstance(row, dict)
        and row.get("component_id") == component_id
        and isinstance(row.get("gate"), str)
        and row["gate"].startswith("compatibility:provider-")
    }
    components = _candidate_components(candidate, "target")
    component = components.get(component_id)
    if component is None or scope["environment_id"] not in environments:
        _fail("relation scope", "environment/component is outside the frozen profile")
    runtimes = {
        row.get("id") for row in profile.get("python_runtimes", []) if isinstance(row, dict)
    }
    if (
        component.get("kind") != "python_distribution"
        or scope["runtime_status"] != "declared"
        or scope["runtime_id"] not in runtimes
    ):
        _fail("relation scope", "only a declared Python runtime scope is supported")
    if not any(
        row.get("component_id") == component_id
        and row.get("environment") == scope["environment_id"]
        and row.get("gate", "").startswith("compatibility:provider-")
        for row in profile.get("acceptance_requirements", [])
        if isinstance(row, dict)
    ):
        _fail("relation scope", "runtime scope is outside profile acceptance requirements")
    return scope


def _find_target_relation(
    candidate: dict[str, Any], profile: dict[str, Any], subject_digest: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for row in candidate.get("compatibility_coverage", []):
        if not isinstance(row, dict) or row.get("status") != "confirmed":
            continue
        component_id = row.get("component_id")
        for fmt in row.get("formats", []):
            for relation in fmt.get("relations", []):
                scope = relation.get("scope")
                if not isinstance(scope, dict):
                    continue
                scoped_row = dict(row)
                scoped_format = dict(fmt)
                scoped_format["relations"] = [relation]
                scoped_row["formats"] = [scoped_format]
                computed = release_manifest._compatibility_subjects(
                    [scoped_row], {component_id: [scope]}
                )
                if subject_digest in computed:
                    matches.append((row, fmt, relation))
    if len(matches) != 1:
        _fail(
            "compatibility_subject_sha256",
            "must identify exactly one relation in the frozen candidate",
        )
    row, fmt, relation = matches[0]
    _profile_scope(candidate, profile, row["component_id"], relation["scope"])
    return row, fmt, relation


def _validate_participant_inventory(
    candidate: dict[str, Any], row: dict[str, Any], fmt: dict[str, Any], label: str
) -> None:
    components = _candidate_components(candidate, label)
    participants = set(fmt.get("producers", [])) | set(fmt.get("consumers", []))
    if (
        not participants
        or not participants <= components.keys()
        or row.get("component_id") not in participants
    ):
        _fail(
            label, "format producer/consumer references are outside the frozen component inventory"
        )


def _validate_relation_links(
    candidate: dict[str, Any], row: dict[str, Any], relation: dict[str, Any], predecessor_id: str
) -> None:
    if relation.get("disposition") != "migrate":
        _fail("compatibility relation", "only migrating relations are supported")
    migration_id, rollback_id = relation.get("migration_id"), relation.get("rollback_id")
    migrations = [
        item for item in candidate.get("migrations", []) if item.get("id") == migration_id
    ]
    rollbacks = [item for item in candidate.get("rollback", []) if item.get("id") == rollback_id]
    if len(migrations) != 1 or len(rollbacks) != 1:
        _fail("compatibility relation", "migration/rollback references are not unique and declared")
    migration, rollback = migrations[0], rollbacks[0]
    if (
        migration.get("from") != relation["predecessor"]["release_id"]
        or migration.get("to") != relation["target"]["component_version"]
        or not migration.get("evidence_required")
        or rollback.get("from_candidate") != "self"
        or rollback.get("to_candidate") != predecessor_id
        or not rollback.get("evidence_required")
    ):
        _fail("compatibility relation", "migration or rollback binding does not match")


def _admission_mapping(value: Any) -> tuple[Admission, dict[str, Any]]:
    if not isinstance(value, Admission):
        _fail("admission", "must be the existing immutable Admission record")
    return value, value.to_mapping()


def validate_compatibility_material_set(
    *,
    descriptor_bytes: bytes,
    predecessor_manifest_bytes: bytes,
    predecessor_artifact_bytes: bytes,
    target_candidate: dict[str, Any],
    target_profile: dict[str, Any],
    target_artifact_bytes: bytes,
    admission: Admission,
    receipt_payload: Mapping[str, Any],
    workload_spec: Mapping[str, Any],
) -> MaterialSetResult:
    """Validate one candidate-external JSON-Schema migration material set.

    The caller supplies every byte snapshot. Receipt checking compares its
    claims to the existing admission digest; signature/provider authentication
    remains outside this pure helper.
    """
    descriptor, material_digest = _canonical_descriptor(descriptor_bytes)
    roles = descriptor["materials"]
    if not isinstance(predecessor_manifest_bytes, bytes):
        _fail("predecessor_manifest", "caller-supplied manifest bytes are required")
    if (
        hashlib.sha256(predecessor_manifest_bytes).hexdigest()
        != roles["predecessor_manifest"]["sha256"]
    ):
        _fail("predecessor_manifest", "SHA-256 does not match")
    predecessor_manifest = _strict_object(predecessor_manifest_bytes, "predecessor manifest")
    predecessor = predecessor_manifest.get("candidate")
    predecessor_profile = predecessor_manifest.get("support_profile")
    decision = predecessor_manifest.get("release_decision")
    if (
        not isinstance(predecessor, dict)
        or not isinstance(predecessor_profile, dict)
        or not isinstance(decision, dict)
    ):
        _fail(
            "predecessor manifest", "candidate, support_profile, and release_decision are required"
        )
    predecessor_id = _identity(predecessor, "candidate_id", "predecessor candidate")
    predecessor_profile_id = _identity(predecessor_profile, "profile_id", "predecessor profile")
    if (
        decision.get("candidate_id") != predecessor_id
        or decision.get("profile_id") != predecessor_profile_id
    ):
        _fail("predecessor manifest", "release decision candidate/profile bindings do not match")
    if decision.get("decision") != "accepted":
        _fail("predecessor manifest", "release decision does not claim accepted status")
    predecessor_source = predecessor.get("source")
    if not isinstance(predecessor_source, dict):
        _fail("predecessor manifest", "candidate source is malformed")
    predecessor_commit = predecessor_source.get("commit")

    if not isinstance(target_candidate, dict) or not isinstance(target_profile, dict):
        _fail("target", "candidate and profile must be objects")
    target_id = _identity(target_candidate, "candidate_id", "target candidate")
    target_profile_id = _identity(target_profile, "profile_id", "target profile")
    target_source = target_candidate.get("source")
    if not isinstance(target_source, dict) or not isinstance(target_source.get("commit"), str):
        _fail("target candidate", "source commit is malformed")
    target_source_sha = target_source["commit"]
    if not isinstance(receipt_payload, Mapping):
        _fail("receipt payload", "must be a mapping of existing receipt claims")
    admission_record, admission_data = _admission_mapping(admission)
    if (
        admission_record.candidate_id != target_id
        or admission_record.source_sha != target_source_sha
        or admission_record.candidate_digest
        != hashlib.sha256(release_manifest.canonical_json_bytes(target_candidate)).hexdigest()
        or admission_record.profile_id != target_profile_id
        or admission_record.profile_digest
        != hashlib.sha256(release_manifest.canonical_json_bytes(target_profile)).hexdigest()
    ):
        _fail(
            "admission", "candidate/profile identity or digest does not match frozen target inputs"
        )
    if (
        admission_record.material_id != MATERIAL_ID
        or admission_record.material_digest != material_digest
    ):
        _fail("admission", "material identity/digest does not match canonical descriptor bytes")
    workload_digest = workload_spec_digest(workload_spec)
    if (
        admission_record.workload_id != WORKLOAD_ID
        or admission_record.workload_digest != workload_digest
    ):
        _fail("admission", "workload identity/digest does not match this validator")

    row, fmt, relation = _find_target_relation(
        target_candidate, target_profile, descriptor["compatibility_subject_sha256"]
    )
    component_id = row["component_id"]
    if (
        relation["predecessor"]["candidate_id"] != predecessor_id
        or relation["predecessor"]["source_commit"] != predecessor_commit
    ):
        _fail(
            "compatibility relation",
            "predecessor candidate/source binding does not match supplied manifest",
        )
    if relation["target"]["source_commit"] != target_source_sha:
        _fail("compatibility relation", "target source binding does not match frozen candidate")
    target_component = _candidate_components(target_candidate, "target").get(component_id)
    if (
        relation["target"]["component_id"] != component_id
        or target_component is None
        or relation["target"]["component_version"] != target_component.get("version")
        or not isinstance(target_component.get("descriptor"), dict)
        or relation["target"]["descriptor_sha256"]
        != target_component.get("descriptor", {}).get("sha256")
    ):
        _fail(
            "compatibility relation", "target component/version/descriptor binding does not match"
        )
    if relation["target"]["format_id"] != fmt.get("format_id") or relation["predecessor"][
        "format_id"
    ] != fmt.get("format_id"):
        _fail("compatibility relation", "format identity does not match frozen relation")
    _validate_relation_links(target_candidate, row, relation, predecessor_id)

    scope = relation["scope"]
    if (
        admission_record.candidate_id != target_id
        or admission_record.profile_id != target_profile_id
        or admission_record.environment_id != scope["environment_id"]
        or admission_record.runtime_id != scope["runtime_id"]
    ):
        _fail("admission", "candidate/profile/environment/runtime scope does not match relation")
    if target_profile_id != target_profile.get("profile_id"):
        _fail("target profile", "identity is invalid")

    predecessor_components = _candidate_components(predecessor, "predecessor")
    target_components = _candidate_components(target_candidate, "target")
    predecessor_row = next(
        (
            item
            for item in predecessor.get("compatibility_coverage", [])
            if isinstance(item, dict) and item.get("component_id") == component_id
        ),
        None,
    )
    if predecessor_row is None or predecessor_row.get("status") != "confirmed":
        _fail("predecessor manifest", "component compatibility inventory is not confirmed")
    predecessor_formats = [
        item
        for item in predecessor_row.get("formats", [])
        if isinstance(item, dict) and item.get("format_id") == fmt.get("format_id")
    ]
    if len(predecessor_formats) != 1:
        _fail("predecessor manifest", "format is not uniquely inventoried")
    _validate_participant_inventory(
        predecessor, predecessor_row, predecessor_formats[0], "predecessor"
    )
    _validate_participant_inventory(target_candidate, row, fmt, "target")
    if component_id not in predecessor_components or component_id not in target_components:
        _fail("artifact roles", "provider component is absent from predecessor or target inventory")

    predecessor_role = roles["predecessor_artifact"]
    if predecessor_role["component_id"] != component_id:
        _fail("predecessor_artifact", "component does not match the compatibility provider")
    target_role_ref = roles["target_artifact"]["file"]
    predecessor_archive = _bound_wheel(
        predecessor,
        predecessor_components,
        predecessor_role["component_id"],
        predecessor_role["file"],
        predecessor_artifact_bytes,
        "predecessor_artifact",
    )
    target_archive = _bound_wheel(
        target_candidate,
        target_components,
        relation["target"]["component_id"],
        target_role_ref,
        target_artifact_bytes,
        "target_artifact",
    )
    try:
        _extract_schema_member(
            predecessor_archive,
            roles["predecessor_contract"]["member_path"],
            roles["predecessor_contract"]["sha256"],
            fmt["format_id"],
            "predecessor_contract",
        )
        _extract_schema_member(
            target_archive,
            roles["target_contract"]["member_path"],
            roles["target_contract"]["sha256"],
            fmt["format_id"],
            "target_contract",
        )
    finally:
        predecessor_archive.close()
        target_archive.close()

    expected_receipt = {
        "admission_id": admission_record.admission_id,
        "admission_digest": admission_digest(admission_record),
        "source_sha": admission_record.source_sha,
        "runtime_id": admission_record.runtime_id,
        "workload_id": WORKLOAD_ID,
        "workload_digest": workload_digest,
        "material_digest": material_digest,
        "environment_id": admission_record.environment_id,
    }
    for key, expected in expected_receipt.items():
        if receipt_payload.get(key) != expected:
            _fail("receipt payload", f"{key} is not bound to the immutable admission")
    if (
        admission_data["candidate_id"] != target_id
        or admission_data["profile_id"] != target_profile_id
    ):
        _fail("admission", "candidate/profile binding is inconsistent")

    return MaterialSetResult(
        relation_subject_sha256=descriptor["compatibility_subject_sha256"],
        material_digest=material_digest,
        workload_id=WORKLOAD_ID,
        workload_digest=workload_digest,
    )
