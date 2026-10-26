from __future__ import annotations

import hashlib
import io
import json
import copy
import stat
import subprocess
import tarfile
import tracemalloc
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from scripts import platform_release_manifest as release_manifest
from scripts import platform_dependency_inputs as dependency_inputs
from scripts import release_obligation_report as release_report_module
from scripts import web_console_release_contract as release_contract
from scripts.platform_dependency_inputs import (
    build_profile_selection_fragment,
    build_dependency_input_report,
    validate_dependency_input_report,
)
from scripts.release_obligation_report import build_obligation_report
from scripts.platform_release_manifest import (
    ManifestError,
    load_manifest,
    object_identity,
    sha256_bytes,
    validate_candidate,
)


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path.as_posix()


def _ref(root: Path, path: Path, origin: str = "source") -> dict[str, str]:
    return {
        "origin": origin,
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _core_metadata(name: str, version: str, metadata_version: str = "2.2") -> bytes:
    return f"Metadata-Version: {metadata_version}\nName: {name}\nVersion: {version}\n\n".encode()


def _write_wheel(bundle: Path, component: dict[str, Any]) -> str:
    name = component["id"]
    version = component["version"]
    wheel_name = name.replace("-", "_")
    wheel_path = bundle / f"dist/{wheel_name}-{version}-py3-none-any.whl"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{wheel_name}-{version}.dist-info/METADATA", _core_metadata(name, version)
        )
        archive.writestr(
            f"{wheel_name}-{version}.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        )
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path.write_bytes(buffer.getvalue())
    return wheel_path.as_posix()


def _write_sdist(bundle: Path, component: dict[str, Any]) -> str:
    name = component["id"]
    version = component["version"]
    root = f"{name}-{version}"
    sdist_path = bundle / f"dist/{root}.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for filename, content in (
            ("PKG-INFO", _core_metadata(name, version)),
            ("pyproject.toml", f'[project]\nname = "{name}"\nversion = "{version}"\n'.encode()),
        ):
            info = tarfile.TarInfo(f"{root}/{filename}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    sdist_path.parent.mkdir(parents=True, exist_ok=True)
    sdist_path.write_bytes(buffer.getvalue())
    return sdist_path.as_posix()


def _build_wheel(
    name: str,
    version: str,
    *,
    filename_name: str | None = None,
    filename_version: str | None = None,
    filename_tag: str = "py3-none-any",
    dist_info_name: str | None = None,
    dist_info_version: str | None = None,
    metadata_name: str | None = None,
    metadata_version: str | None = None,
    metadata_format_version: str = "2.2",
    metadata_requires_python: list[str] | None = None,
    wheel_metadata: bytes | None = None,
    extra_members: list[tuple[str, bytes, int | None]] | None = None,
) -> tuple[str, bytes]:
    filename_name = filename_name or name.replace("-", "_")
    filename_version = filename_version or version
    dist_info_name = dist_info_name or name.replace("-", "_")
    dist_info_version = dist_info_version or version
    filename = f"{filename_name}-{filename_version}-{filename_tag}.whl"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        metadata_path = f"{dist_info_name}-{dist_info_version}.dist-info/METADATA"
        metadata_content = _core_metadata(
            metadata_name or name, metadata_version or version, metadata_format_version
        )
        if metadata_requires_python:
            metadata_content = (
                metadata_content.rstrip(b"\n")
                + b"\n"
                + b"".join(
                    f"Requires-Python: {value}\n".encode() for value in metadata_requires_python
                )
                + b"\n"
            )
        archive.writestr(metadata_path, metadata_content)
        archive.writestr(
            f"{dist_info_name}-{dist_info_version}.dist-info/WHEEL",
            wheel_metadata
            if wheel_metadata is not None
            else b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
        )
        for member_name, content, mode in extra_members or []:
            info = zipfile.ZipInfo(member_name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return filename, buffer.getvalue()


def _build_sdist(
    name: str,
    version: str,
    *,
    filename_name: str | None = None,
    filename_version: str | None = None,
    root_name: str | None = None,
    root_version: str | None = None,
    metadata_name: str | None = None,
    metadata_version: str | None = None,
    pyproject_name: str | None = None,
    pyproject_version: str | None = None,
    metadata_format_version: str = "2.2",
    pyproject_content: bytes | None = None,
    extra_members: list[tuple[str, bytes, bytes | None]] | None = None,
) -> tuple[str, bytes]:
    filename = f"{filename_name or name}-{filename_version or version}.tar.gz"
    root = f"{root_name or name}-{root_version or version}"
    pkg_info = _core_metadata(
        metadata_name or name, metadata_version or version, metadata_format_version
    )
    pyproject = (
        pyproject_content
        or (
            f'[project]\nname = "{pyproject_name or name}"\n'
            f'version = "{pyproject_version or version}"\n'
        ).encode()
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member_name, content in (
            (f"{root}/PKG-INFO", pkg_info),
            (f"{root}/pyproject.toml", pyproject),
        ):
            info = tarfile.TarInfo(member_name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for member_name, content, member_type in extra_members or []:
            info = tarfile.TarInfo(member_name)
            if member_type is None:
                assert content is not None
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                info.type = member_type
                archive.addfile(info)
    return filename, buffer.getvalue()


def _replace_python_wheel(
    bundle: Path, payload: dict[str, Any], component_id: str, filename: str, content: bytes
) -> None:
    artifact = next(
        item
        for item in payload["candidate"]["artifacts"]
        if item["component"] == component_id and item["file"]["path"].endswith(".whl")
    )
    path = bundle / "dist" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    artifact["file"] = _ref(bundle, path, "bundle")
    _refresh(payload, bundle)


def _replace_python_sdist(
    bundle: Path, payload: dict[str, Any], component_id: str, filename: str, content: bytes
) -> None:
    artifact = next(
        item
        for item in payload["candidate"]["artifacts"]
        if item["component"] == component_id and item["file"]["path"].endswith(".tar.gz")
    )
    path = bundle / "dist" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    artifact["file"] = _ref(bundle, path, "bundle")
    _refresh(payload, bundle)


def _refresh(
    payload: dict[str, Any],
    bundle: Path | None = None,
    refresh_receipts: bool = True,
    refresh_runtime_receipts: bool = True,
) -> None:
    candidate = payload["candidate"]
    profile = payload["support_profile"]
    if payload.get("schema_version") == 3 and bundle is not None:
        providers = profile["python_dependency_selection"]["providers"]
        selection = profile["python_dependency_selection"]
        selection_digest = sha256_bytes(
            json.dumps(
                selection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        policy_path = bundle / "receipts/resolver-index-policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_bytes = b'{"schema_version":1,"policy":"fixture-index-policy"}\n'
        policy_path.write_bytes(policy_bytes)
        policy_ref = _ref(bundle, policy_path, "bundle")
        descriptor_refs = sorted(
            (
                {"component_id": component["id"], "descriptor": component["descriptor"]}
                for component in candidate["components"]
                if component["kind"] == "python_distribution"
            ),
            key=lambda row: row["component_id"],
        )
        source_root = bundle.parent / "repo"
        requirements_by_component: dict[str, list[str]] = {}
        for row in descriptor_refs:
            project_data = tomllib.loads(
                (source_root / row["descriptor"]["path"]).read_text(encoding="utf-8")
            )
            project = project_data["project"]
            requirements = list(project.get("dependencies", []))
            selected = next(
                (
                    provider["extras"]
                    for provider in providers
                    if provider["component_id"] == row["component_id"]
                ),
                [],
            )
            optional = project.get("optional-dependencies", {})
            for extra in selected:
                requirements.extend(optional.get(extra, []))
            requirements_by_component[row["component_id"]] = requirements
        runtimes = {row["id"]: row for row in profile["python_runtimes"]}
        for lock in candidate["locks"]:
            if lock["ecosystem"] != "python" or lock["purpose"] != "runtime":
                continue
            receipt_path = bundle / f"receipts/{lock['id']}-runtime-resolution.json"
            lock["python_runtime_resolution_receipt"] = {
                "origin": "bundle",
                "path": receipt_path.relative_to(bundle).as_posix(),
                "sha256": lock.get("python_runtime_resolution_receipt", {}).get("sha256", "0" * 64),
            }
            runtime = runtimes[lock["python_runtime_id"]]
            resolver_input = release_manifest._runtime_resolver_input_bytes(
                requirements_by_component, descriptor_refs, selection, runtime
            )
            input_path = bundle / f"receipts/{lock['id']}-effective-input.json"
            if refresh_runtime_receipts:
                input_path.write_bytes(resolver_input)
            receipt = {
                "schema_version": 1,
                "contract_type": "python_runtime_resolution_v1",
                "source_commit": candidate["source"]["commit"],
                "selection_sha256": selection_digest,
                "source_descriptors": descriptor_refs,
                "resolver_input": _ref(bundle, input_path, "bundle"),
                "python_runtime_id": runtime["id"],
                "observed_runtime": {"implementation": "cpython", "version": runtime["version"]},
                "target_environment": lock["target_environment"],
                "resolver": lock["resolver"],
                "resolver_version": lock["version"],
                "resolver_index_policy": policy_ref,
                "lock_sha256": lock["file"]["sha256"],
                "outcome": "resolved",
                "run_id": f"fixture-resolution-{lock['id']}",
                "attempt": 1,
            }
            if refresh_runtime_receipts:
                raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
                receipt_path.write_bytes(raw)
                lock["python_runtime_resolution_receipt"]["sha256"] = sha256_bytes(raw)
    candidate["candidate_id"] = object_identity(candidate, "candidate_id")
    profile["profile_id"] = object_identity(profile, "profile_id")
    for evidence in payload["evidence"]:
        evidence["candidate_id"] = candidate["candidate_id"]
        evidence["profile_id"] = profile["profile_id"]
        evidence["source_commit"] = candidate["source"]["commit"]
        evidence["evidence_sha256"] = object_identity(evidence, "evidence_sha256")
    receipt_refs: list[str] = []
    if bundle is not None and not refresh_receipts:
        receipt_refs = [row["receipt"]["sha256"] for row in payload.get("lock_reconstructions", [])]
    if bundle is not None and refresh_receipts:
        locks = {item["id"]: item for item in candidate["locks"]}
        runtimes = {item["id"]: item for item in profile.get("python_runtimes", [])}
        for row in payload.get("lock_reconstructions", []):
            lock = locks[row["lock_id"]]
            runtime = runtimes[row["python_runtime_id"]]
            receipt = {
                "schema_version": 1,
                "contract_type": "python_lock_reconstruction_v1",
                "candidate_id": candidate["candidate_id"],
                "profile_id": profile["profile_id"],
                "source_commit": candidate["source"]["commit"],
                "lock_id": lock["id"],
                "lock_sha256": lock["file"]["sha256"],
                "environment": row["environment"],
                "python_runtime_id": runtime["id"],
                "observed_runtime": {"implementation": "cpython", "version": runtime["version"]},
                "run_id": f"fixture-{lock['id']}-{runtime['id']}",
                "attempt": 1,
                "outcome": "passed",
                "resolved_distributions": [
                    {"name": name, "version": version}
                    for name, version in release_manifest._validate_hashed_requirements_v1(
                        (bundle / lock["file"]["path"]).read_bytes(), lock["id"]
                    )
                    if name
                    not in {
                        release_manifest.canonicalize_name(item["id"])
                        for item in candidate["components"]
                        if item["kind"] == "python_distribution"
                    }
                ],
            }
            path = bundle / row["receipt"]["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
            path.write_bytes(raw)
            row["receipt"]["sha256"] = sha256_bytes(raw)
            receipt_refs.append(row["receipt"]["sha256"])
    if payload.get("build_contexts"):
        _refresh_typed_build_links(payload)
    decision = payload["release_decision"]
    decision["candidate_id"] = candidate["candidate_id"]
    decision["profile_id"] = profile["profile_id"]
    decision["evidence_refs"] = [
        item["evidence_sha256"] for item in payload["evidence"]
    ] + receipt_refs


def _refresh_typed_build_links(payload: dict[str, Any]) -> None:
    locks = {row["id"]: row for row in payload["candidate"]["locks"]}
    receipts = {
        (row["lock_id"], row["python_runtime_id"]): row["receipt"]["sha256"]
        for row in payload.get("lock_reconstructions", [])
    }
    context_digests: dict[str, str] = {}
    for context in payload.get("build_contexts", []):
        old_digest = context["context_sha256"]
        bootstrap = locks[context["bootstrap_lock_id"]]
        context["bootstrap_lock_sha256"] = bootstrap["file"]["sha256"]
        context["bootstrap_reconstruction_receipt_sha256"] = receipts[
            (bootstrap["id"], context["python_runtime_id"])
        ]
        context["context_sha256"] = object_identity(context, "context_sha256")
        context_digests[old_digest] = context["context_sha256"]

    hook_digests: dict[str, str] = {}
    for hook in payload.get("hook_attestations", []):
        old_digest = hook["attestation_sha256"]
        hook["context_sha256"] = context_digests[hook["context_sha256"]]
        lock = locks[hook["final_lock_id"]]
        hook["final_lock_sha256"] = lock["file"]["sha256"]
        hook["reconstruction_receipt_sha256"] = receipts[
            (
                lock["id"],
                next(
                    context["python_runtime_id"]
                    for context in payload["build_contexts"]
                    if context["context_sha256"] == hook["context_sha256"]
                ),
            )
        ]
        hook["attestation_sha256"] = object_identity(hook, "attestation_sha256")
        hook_digests[old_digest] = hook["attestation_sha256"]

    for build in payload.get("build_attestations", []):
        build["context_sha256"] = context_digests[build["context_sha256"]]
        build["hook_attestation_sha256"] = hook_digests[build["hook_attestation_sha256"]]
        lock = locks[build["final_lock_id"]]
        build["final_lock_sha256"] = lock["file"]["sha256"]
        context = next(
            row
            for row in payload["build_contexts"]
            if row["context_sha256"] == build["context_sha256"]
        )
        build["reconstruction_receipt_sha256"] = receipts[
            (lock["id"], context["python_runtime_id"])
        ]
        build["attestation_sha256"] = object_identity(build, "attestation_sha256")


def _replace_lock(
    bundle: Path,
    payload: dict[str, Any],
    lock_id: str,
    content: bytes,
    *,
    refresh_receipts: bool = True,
) -> None:
    lock = next(item for item in payload["candidate"]["locks"] if item["id"] == lock_id)
    lock_path = bundle / lock["file"]["path"]
    lock_path.write_bytes(content)
    lock["file"]["sha256"] = sha256_bytes(content)
    _refresh(payload, bundle, refresh_receipts=refresh_receipts)


def _rewrite_receipt(
    bundle: Path, payload: dict[str, Any], row: dict[str, Any], mutate: Any
) -> None:
    path = bundle / row["receipt"]["path"]
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutate(receipt)
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    row["receipt"]["sha256"] = sha256_bytes(raw)


@pytest.fixture
def repo_case(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    return _make_repo_case(tmp_path)


def _make_repo_case(
    tmp_path: Path,
    unresolved_roles: set[str] | None = None,
    bad_dependency_reference: str | None = None,
    schema_ref_mutation: Any = None,
    schema_nested_id: bool = False,
    moved_catalog_roots: bool = False,
    schema_id_mismatch: bool = False,
    schema_package_mismatch: bool = False,
    schema_extra_file: bool = False,
    python_requires: str = ">=3.12",
    python_dependencies: list[str] | None = None,
    python_optional_dependencies: dict[int, dict[str, list[str]]] | None = None,
    dynamic_optional_dependencies: set[int] | None = None,
    include_registry_pyproject: bool = False,
) -> tuple[Path, Path, dict[str, Any]]:
    unresolved_roles = unresolved_roles or set()
    python_optional_dependencies = python_optional_dependencies or {}
    dynamic_optional_dependencies = dynamic_optional_dependencies or set()
    root = tmp_path / "repo"
    root.mkdir()
    bundle = tmp_path / "candidate-bundle"
    bundle.mkdir()
    _write(
        root / "schemas/platform-release-manifest-v2.schema.json",
        Path("schemas/platform-release-manifest-v2.schema.json").read_text(),
    )
    _write(
        root / "schemas/platform-release-manifest-v3.schema.json",
        Path("schemas/platform-release-manifest-v3.schema.json").read_text(),
    )
    _write(
        root / "schemas/platform-compatibility-coverage-policy-v1.json",
        Path("schemas/platform-compatibility-coverage-policy-v1.json").read_text(),
    )
    coverage_policy = json.loads(
        (root / "schemas/platform-compatibility-coverage-policy-v1.json").read_text()
    )
    component_specs: list[tuple[str, str]] = []
    for index in range(26):
        name = f"fixture-distribution-{index:02d}"
        descriptor = f"modules/{index:02d}/pyproject.toml"
        dependencies = (
            f"dependencies = {json.dumps(python_dependencies or [])}\n" if index == 0 else ""
        )
        optional = ""
        if index in dynamic_optional_dependencies:
            optional = 'dynamic = ["optional-dependencies"]\n'
        elif python_optional_dependencies.get(index):
            optional = "[project.optional-dependencies]\n" + "".join(
                f"{group} = {json.dumps(requirements)}\n"
                for group, requirements in sorted(python_optional_dependencies[index].items())
            )
        _write(
            root / descriptor,
            f'[project]\nname = "{name}"\nversion = "1.0.{index}"\nrequires-python = "{python_requires}"\n{dependencies}{optional}'
            '[build-system]\nrequires = []\nbuild-backend = "fixture.backend"\n',
        )
        source = f"modules/{index:02d}/src/{name.replace('-', '_')}/__init__.py"
        _write(root / source, "VALUE = 1\n")
        component_specs.append((name, descriptor))
    project_root = Path(__file__).resolve().parents[1]
    console_path = "web/market-data-console/package.json"
    console_manifest = (project_root / "web/market-data-console/package.json").read_text(
        encoding="utf-8"
    )
    console_metadata = json.loads(console_manifest)
    _write(root / console_path, console_manifest)
    refs = {
        path: _write(root / path, f"{path} fixture\n")
        for path in (
            "contracts/compatibility.json",
            "migrations/upgrade.py",
            "runbooks/rollback.md",
            "composition.json",
            "profiles/topology.json",
            "profiles/host.json",
            "profiles/acceptance.json",
        )
    }
    policy = json.loads(
        (project_root / "schemas/results-registry-compatibility-policy-v1.json").read_text()
    )
    registry_root = project_root / "modules/results_aggregator"
    if include_registry_pyproject:
        _write(
            root / "modules/results_aggregator/pyproject.toml",
            (registry_root / "pyproject.toml").read_text(encoding="utf-8"),
        )
    registry_files = [
        registry_root / "schemas/catalog.json",
        *sorted((registry_root / "schemas").glob("*.schema.json")),
        registry_root / "src/modular_results_registry/schemas/catalog.json",
        *sorted((registry_root / "src/modular_results_registry/schemas").glob("*.schema.json")),
    ]
    if moved_catalog_roots:
        policy["source_catalog_path"] = "relocated/source/schemas/catalog.json"
        policy["package_catalog_path"] = "relocated/package/schemas/catalog.json"
        for row in policy["schemas"]:
            name = Path(row["path"]).name
            row["path"] = f"relocated/source/schemas/{name}"
            row["package_path"] = f"relocated/package/schemas/{name}"
    for source_file in registry_files:
        name = source_file.name
        is_package_copy = "src/modular_results_registry/schemas" in source_file.as_posix()
        if is_package_copy:
            relative = policy["package_catalog_path"].replace("catalog.json", name)
        else:
            relative = policy["source_catalog_path"].replace("catalog.json", name)
        content = source_file.read_bytes()
        if (
            schema_ref_mutation is not None or schema_nested_id or schema_id_mismatch
        ) and name == "artifact-record.schema.json":
            document = json.loads(content)
            if schema_ref_mutation is not None:
                document["$ref"] = schema_ref_mutation
                document["examples"] = [{"$anchor": "fake"}]
            if schema_nested_id:
                document["properties"]["nested"] = {"$id": "nested-resource.json", "type": "object"}
            if schema_id_mismatch:
                document["$id"] = "urn:modular-results-registry:wrong-id:1"
            content = (json.dumps(document, indent=2) + "\n").encode()
            matching = next(
                row for row in policy["schemas"] if row["id"].endswith(":artifact-record:1")
            )
            matching["content_sha256"] = hashlib.sha256(content).hexdigest()
        if schema_package_mismatch and is_package_copy and name == "artifact-record.schema.json":
            content += b" "
        _write(root / relative, content.decode())
        _write(bundle / relative, content.decode())
    if schema_extra_file:
        extra_root = root / policy["source_catalog_path"].removesuffix("/catalog.json")
        _write(
            extra_root / "unlisted.schema.json", '{"$id":"urn:fixture:unlisted","type":"object"}\n'
        )

    def canonical(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    entries = sorted(
        (
            {"id": row["id"], "path": row["path"], "content_sha256": row["content_sha256"]}
            for row in policy["schemas"]
        ),
        key=lambda entry: (entry["id"], entry["path"]),
    )
    policy["schema_set_sha256"] = hashlib.sha256(canonical(entries)).hexdigest()
    policy_bytes = (json.dumps(policy, indent=2) + "\n").encode()
    _write(root / "schemas/results-registry-compatibility-policy-v1.json", policy_bytes.decode())
    _write(root / refs["contracts/compatibility.json"], policy_bytes.decode())
    capability_names = [
        "python_distributions",
        "market_data_web_console",
        "operator_ui_control",
        "recovery_ui",
        "owner_native_composition",
        "topology",
        "real_host",
        "lifecycle",
        "drift",
        "risk_supervisor",
        "portfolio",
        "security",
        "runbooks",
        "recovery",
        "ab_soak",
        "soak_2h",
        "soak_24h",
    ]
    console_id = console_metadata["name"]
    console_version = console_metadata["version"]
    component_ids = [name for name, _ in component_specs] + [console_id]
    inventory_roles: list[dict[str, Any]] = []
    for index, capability_name in enumerate(capability_names):
        if capability_name == "python_distributions":
            providers = component_ids[:26]
            gates = [
                "gate_00",
                "migration:migration-v1",
                "rollback:rollback-v1",
                "lock:python-build-312",
                "lock:python-build-313",
                "lock:python-bootstrap-312",
                "lock:python-bootstrap-313",
                "lock:python-runtime-312",
                "lock:python-runtime-313",
                "rebuild:python-build-312",
                "rebuild:python-build-313",
                "rebuild:python-bootstrap-312",
                "rebuild:python-bootstrap-313",
                "rebuild:python-runtime-312",
                "rebuild:python-runtime-313",
            ]
        elif capability_name == "market_data_web_console":
            providers = [console_id]
            gates = ["gate_01"]
        elif capability_name == "real_host":
            providers = [component_ids[index % len(component_ids)]]
            gates = ["operations:mac-service-reconnect"]
        else:
            providers = [component_ids[index % len(component_ids)]]
            gates = [f"gate_{index:02d}"]
        blocked = capability_name in unresolved_roles
        inventory_roles.append(
            {
                "id": capability_name,
                "capability": capability_name,
                "required": True,
                "providers": providers,
                "required_gates": gates,
                "blocked_by": (
                    [
                        {
                            "type": "github_issue",
                            "url": bad_dependency_reference
                            or (
                                "https://github.com/MishkaStrategy/Module-Strategy/issues/778"
                                if capability_name == "recovery_ui"
                                else "https://github.com/MishkaStrategy/Module-Strategy/issues/396"
                            ),
                        }
                    ]
                    if blocked
                    else []
                ),
            }
        )
    inventory_path = "profiles/component-inventory.json"
    inventory_text = (
        json.dumps(
            {"schema_version": 1, "roles": inventory_roles}, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    refs[inventory_path] = _write(root / inventory_path, inventory_text)
    hashed_requirements = f"fixture-dependency==1.0.0 \\\n  --hash=sha256:{'a' * 64}\n"
    for lock_name in (
        "python-bootstrap-312",
        "python-bootstrap-313",
        "python-runtime-312",
        "python-runtime-313",
        "python-build-312",
        "python-build-313",
    ):
        _write(bundle / f"locks/{lock_name}.lock", hashed_requirements)
    npm_lock = (project_root / "web/market-data-console/package-lock.json").read_text(
        encoding="utf-8"
    )
    _write(root / "web/market-data-console/package-lock.json", npm_lock)
    _write(bundle / "locks/npm-lock.json", npm_lock)
    symlink_target = root / "modules/00/src/fixture_distribution_00/__init__.py"
    (root / "linked-source.py").symlink_to(symlink_target)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "remote",
            "add",
            "origin",
            "https://github.com/MishkaStrategy/Module-Strategy.git",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture source"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    components = [
        {
            "id": name,
            "kind": "python_distribution",
            "version": f"1.0.{index}",
            "descriptor": _ref(root, root / descriptor),
            "source_files": [
                _ref(root, root / f"modules/{index:02d}/src/{name.replace('-', '_')}/__init__.py")
            ],
        }
        for index, (name, descriptor) in enumerate(component_specs)
    ]
    components.append(
        {
            "id": "@module-strategy/market-data-console",
            "kind": "web_console",
            "version": console_version,
            "descriptor": _ref(root, root / console_path),
            "source_files": [_ref(root, root / console_path)],
        }
    )
    artifacts: list[dict[str, Any]] = []
    for index, component in enumerate(components):
        if component["kind"] == "python_distribution":
            artifact_path = Path(_write_wheel(bundle, component))
        else:
            artifact_path = bundle / f"dist/component-{index:02d}.bin"
            _write(artifact_path, f"artifact for {component['id']}\n")
        artifacts.append(
            {
                "component": component["id"],
                "kind": "distribution",
                "file": _ref(bundle, artifact_path, "bundle"),
            }
        )
    artifact_sha_by_component = {item["component"]: item["file"]["sha256"] for item in artifacts}
    caps = {
        role["capability"]: {
            "status": "blocked" if role["id"] in unresolved_roles else "supported",
            "components": role["providers"],
            "required_gates": role["required_gates"],
        }
        for role in inventory_roles
    }
    candidate: dict[str, Any] = {
        "candidate_id": "0" * 64,
        "source": {
            "repository": "https://github.com/MishkaStrategy/Module-Strategy.git",
            "commit": commit,
            "clean": True,
        },
        "components": components,
        "locks": [
            {
                "id": f"python-{purpose}-{minor}",
                "ecosystem": "python",
                "python_runtime_id": f"cpython-{version}",
                "format": "requirements-hashed-v1",
                "resolver": "uv",
                "version": "0.11.31",
                "purpose": purpose,
                "covered_components": [
                    item["id"] for item in components if item["kind"] == "python_distribution"
                ],
                "target_environment": "owner-defined",
                "file": _ref(bundle, bundle / f"locks/python-{purpose}-{minor}.lock", "bundle"),
            }
            for purpose in ("bootstrap", "build", "runtime")
            for minor, version in (("312", "3.12.8"), ("313", "3.13.1"))
        ]
        + [
            {
                "id": "console-runtime",
                "ecosystem": "node",
                "format": "npm-lock-v3",
                "resolver": "npm",
                "version": "11.17.0",
                "purpose": "build",
                "covered_components": ["@module-strategy/market-data-console"],
                "target_environment": "owner-defined",
                "file": _ref(bundle, bundle / "locks/npm-lock.json", "bundle"),
            },
        ],
        "artifacts": artifacts,
        "compatibility": [
            {
                "id": "compat-v1",
                "from": "v1",
                "to": "v2",
                "contract": _ref(root, root / refs["contracts/compatibility.json"]),
                "evidence_required": False,
            }
        ],
        "compatibility_coverage": [
            {
                "component_id": component_id,
                "status": "unknown-blocking",
                "inventory_status": "unknown",
                "owner": None,
                "formats": [],
            }
            for component_id in component_ids
        ],
        "migrations": [
            {
                "id": "migration-v1",
                "kind": "config_conversion",
                "from": "v1",
                "to": "v2",
                "implementation": _ref(root, root / refs["migrations/upgrade.py"]),
                "evidence_required": True,
            }
        ],
        "rollback": [
            {
                "id": "rollback-v1",
                "from_candidate": "0" * 64,
                "to_candidate": "1" * 64,
                "procedure": _ref(root, root / refs["runbooks/rollback.md"]),
                "evidence_required": True,
            }
        ],
        "composition": _ref(root, root / refs["composition.json"]),
    }
    profile: dict[str, Any] = {
        "profile_id": "0" * 64,
        "name": "fixture profile",
        "target_environments": ["owner-defined"],
        "acceptance_requirements": [
            {
                "gate": gate,
                "component_id": component_id,
                "environment": "owner-defined",
            }
            for role in inventory_roles
            for component_id in role["providers"]
            for gate in role["required_gates"]
            if not gate.startswith(("lock:", "rebuild:"))
        ]
        + [
            {
                "gate": release_manifest._compatibility_provider_gate(
                    component_id, coverage_policy
                ),
                "component_id": component_id,
                "environment": "owner-defined",
            }
            for role in inventory_roles
            if role["capability"] in coverage_policy["required_capabilities"]
            for component_id in role["providers"]
        ],
        "python_runtimes": [
            {"id": "cpython-3.12.8", "implementation": "cpython", "version": "3.12.8"},
            {"id": "cpython-3.13.1", "implementation": "cpython", "version": "3.13.1"},
        ],
        "lock_requirements": [
            {
                "component_id": item["id"],
                "ecosystem": "python",
                "purpose": purpose,
                "target_environment": "owner-defined",
                "python_runtime_id": runtime_id,
            }
            for item in components
            if item["kind"] == "python_distribution"
            for purpose in ("bootstrap", "build", "runtime")
            for runtime_id in ("cpython-3.12.8", "cpython-3.13.1")
        ]
        + [
            {
                "component_id": "@module-strategy/market-data-console",
                "ecosystem": "node",
                "purpose": "build",
                "target_environment": "owner-defined",
            }
        ],
        "no_lock_components": [],
        "topology": _ref(root, root / refs["profiles/topology.json"]),
        "host_profile": _ref(root, root / refs["profiles/host.json"]),
        "build_tools": [],
        "component_inventory": _ref(root, root / refs[inventory_path]),
        "capabilities": caps,
        "acceptance_policy": _ref(root, root / refs["profiles/acceptance.json"]),
    }
    payload: dict[str, Any] = {
        "schema_version": 2,
        "candidate": candidate,
        "support_profile": profile,
        "evidence": [],
        "lock_reconstructions": [
            {
                "lock_id": f"python-{purpose}-{minor}",
                "python_runtime_id": f"cpython-{version}",
                "environment": "owner-defined",
                "receipt": {
                    "origin": "bundle",
                    "path": f"receipts/python-{purpose}-{minor}.json",
                    "sha256": "0" * 64,
                },
            }
            for purpose in ("bootstrap", "build", "runtime")
            for minor, version in (("312", "3.12.8"), ("313", "3.13.1"))
        ],
        "release_decision": {
            "decision": "pending",
            "candidate_id": "0" * 64,
            "profile_id": "0" * 64,
            "evidence_refs": [],
            "owner_approval": {
                "owner": "fixture owner",
                "approved_at": "2026-01-01T00:00:00Z",
                "decision": "pending",
                "approval_sha256": "a" * 64,
            },
        },
    }
    _refresh(payload, bundle)
    for role in inventory_roles:
        for gate in role["required_gates"]:
            if gate.startswith(("lock:", "rebuild:")):
                continue
            for component_id in role["providers"]:
                evidence = {
                    "candidate_id": candidate["candidate_id"],
                    "profile_id": profile["profile_id"],
                    "source_commit": commit,
                    "component_id": component_id,
                    "artifact_sha256": artifact_sha_by_component[component_id],
                    "gate": gate,
                    "environment": "owner-defined",
                    "run_id": f"fixture-{gate}-{component_id}",
                    "attempt": 1,
                    "outcome": "passed",
                    "evidence_sha256": "0" * 64,
                }
                evidence["evidence_sha256"] = object_identity(evidence, "evidence_sha256")
                payload["evidence"].append(evidence)
    for role in inventory_roles:
        if role["capability"] not in coverage_policy["required_capabilities"]:
            continue
        for component_id in role["providers"]:
            gate = release_manifest._compatibility_provider_gate(component_id, coverage_policy)
            evidence = {
                "candidate_id": candidate["candidate_id"],
                "profile_id": profile["profile_id"],
                "source_commit": commit,
                "component_id": component_id,
                "artifact_sha256": artifact_sha_by_component[component_id],
                "gate": gate,
                "environment": "owner-defined",
                "run_id": f"fixture-{gate}-{component_id}",
                "attempt": 1,
                "outcome": "passed",
                "evidence_sha256": "0" * 64,
            }
            evidence["evidence_sha256"] = object_identity(evidence, "evidence_sha256")
            payload["evidence"].append(evidence)
    _refresh(payload, bundle)
    return root, bundle, payload


def _accepted(
    payload: dict[str, Any], bundle: Path | None = None, *, with_build_evidence: bool = True
) -> None:
    decision = payload["release_decision"]
    decision["decision"] = "accepted"
    decision["owner_approval"]["decision"] = "accepted"
    if bundle is not None:
        existing_sdists = {
            artifact["component"]
            for artifact in payload["candidate"]["artifacts"]
            if artifact["file"]["path"].endswith(".tar.gz")
        }
        for component in payload["candidate"]["components"]:
            if component["kind"] != "python_distribution":
                continue
            if component["id"] in existing_sdists:
                continue
            sdist_path = Path(_write_sdist(bundle, component))
            payload["candidate"]["artifacts"].append(
                {
                    "component": component["id"],
                    "kind": "distribution",
                    "file": _ref(bundle, sdist_path, "bundle"),
                }
            )
    for row in payload["candidate"]["compatibility_coverage"]:
        row["status"] = "confirmed"
        row["inventory_status"] = "none"
        row["owner"] = "fixture owner confirmation"
        row["formats"] = []
        _attach_compatibility_subject_evidence(payload, row)
    _refresh(payload, bundle)
    if bundle is not None and with_build_evidence:
        _add_accepted_build_evidence(bundle, payload)


def _allow_synthetic_console_runtime_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give accepted-path fixtures a declared synthetic console runtime scope.

    This test-only scope lets unrelated downstream accepted-decision checks run;
    it adds no runtime to the manifest/profile. The unpatched fail-closed policy
    remains covered by test_accepted_decision_rejects_unresolved_console_runtime_scope.
    """
    original = release_manifest._compatibility_scope_keys

    def synthetic_scope_keys(
        component_id: str,
        components_by_id: dict[str, dict[str, Any]],
        profile: dict[str, Any],
        acceptance_rows: list[dict[str, Any]],
        compatibility_gates: dict[str, str],
    ) -> list[dict[str, str | None]]:
        scopes = original(
            component_id, components_by_id, profile, acceptance_rows, compatibility_gates
        )
        if component_id != "@module-strategy/market-data-console":
            return scopes
        return [
            {
                **scope,
                "runtime_id": "fixture-console-runtime",
                "runtime_status": "declared",
            }
            for scope in scopes
        ]

    monkeypatch.setattr(release_manifest, "_compatibility_scope_keys", synthetic_scope_keys)


def _add_accepted_build_evidence(bundle: Path, payload: dict[str, Any]) -> None:
    """Add structurally valid synthetic wheel and sdist evidence for acceptance fixtures."""
    pin_content = (
        f"fixture-dependency==1.0.0 --hash=sha256:{'a' * 64}\n"
        f"fixture-backend==1.0.0 --hash=sha256:{'b' * 64}\n"
        f"fixture-frontend==1.0.0 --hash=sha256:{'c' * 64}\n"
    ).encode()
    for lock_id in (
        "python-bootstrap-312",
        "python-bootstrap-313",
        "python-build-312",
        "python-build-313",
    ):
        _replace_lock(bundle, payload, lock_id, pin_content)

    locks = {row["id"]: row for row in payload["candidate"]["locks"]}
    receipts = {
        (row["lock_id"], row["python_runtime_id"]): row["receipt"]["sha256"]
        for row in payload["lock_reconstructions"]
    }
    runtimes = {row["id"]: row for row in payload["support_profile"]["python_runtimes"]}
    contexts: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []
    builds: list[dict[str, Any]] = []
    for component in payload["candidate"]["components"]:
        if component["kind"] != "python_distribution":
            continue
        for runtime_id, runtime in runtimes.items():
            minor = (
                f"{Version(runtime['version']).release[0]}{Version(runtime['version']).release[1]}"
            )
            bootstrap = locks[f"python-bootstrap-{minor}"]
            build_lock = locks[f"python-build-{minor}"]
            context: dict[str, Any] = {
                "schema_version": 1,
                "context_sha256": "0" * 64,
                "source_commit": payload["candidate"]["source"]["commit"],
                "component_id": component["id"],
                "component_version": component["version"],
                "descriptor": component["descriptor"],
                "backend": {
                    "entrypoint": "fixture.backend",
                    "distribution": "fixture-backend",
                    "version": "1.0.0",
                    "artifact_sha256": "b" * 64,
                },
                "backend_path": [],
                "python_runtime_id": runtime_id,
                "target": {
                    "environment": "owner-defined",
                    "os": "fixture-os",
                    "architecture": "fixture-arch",
                    "abi": "fixture-abi",
                },
                "config_settings": {},
                "frontend": {"name": "fixture-frontend", "version": "1.0.0"},
                "toolchain": [
                    {"name": "fixture-frontend", "version": "1.0.0", "artifact_sha256": "c" * 64}
                ],
                "environment": {"identity": "owner-defined", "isolation": "isolated"},
                "resolver_policy": "fixture-policy",
                "bootstrap_lock_id": bootstrap["id"],
                "bootstrap_lock_sha256": bootstrap["file"]["sha256"],
                "bootstrap_reconstruction_receipt_sha256": receipts[(bootstrap["id"], runtime_id)],
            }
            context["context_sha256"] = object_identity(context, "context_sha256")
            contexts.append(context)
            for operation, suffix in (("wheel", ".whl"), ("sdist", ".tar.gz")):
                receipt_sha256 = receipts[(build_lock["id"], runtime_id)]
                hook: dict[str, Any] = {
                    "schema_version": 1,
                    "attestation_sha256": "0" * 64,
                    "context_sha256": context["context_sha256"],
                    "operation": operation,
                    "status": "returned",
                    "returned_requirements": [],
                    "final_lock_id": build_lock["id"],
                    "final_lock_sha256": build_lock["file"]["sha256"],
                    "reconstruction_receipt_sha256": receipt_sha256,
                    "run_identity": f"fixture-hook-{component['id']}-{runtime_id}-{operation}",
                }
                hook["attestation_sha256"] = object_identity(hook, "attestation_sha256")
                hooks.append(hook)
                artifact = next(
                    row
                    for row in payload["candidate"]["artifacts"]
                    if row["component"] == component["id"] and row["file"]["path"].endswith(suffix)
                )
                build: dict[str, Any] = {
                    "schema_version": 1,
                    "attestation_sha256": "0" * 64,
                    "context_sha256": context["context_sha256"],
                    "hook_attestation_sha256": hook["attestation_sha256"],
                    "operation": operation,
                    "python_runtime_id": runtime_id,
                    "target_environment": "owner-defined",
                    "final_lock_id": build_lock["id"],
                    "final_lock_sha256": build_lock["file"]["sha256"],
                    "reconstruction_receipt_sha256": receipt_sha256,
                    "outcome": "passed",
                    "run_identity": f"fixture-build-{component['id']}-{runtime_id}-{operation}",
                    "artifact": dict(artifact["file"]),
                }
                build["attestation_sha256"] = object_identity(build, "attestation_sha256")
                builds.append(build)
    payload["build_contexts"] = contexts
    payload["hook_attestations"] = hooks
    payload["build_attestations"] = builds


def test_valid_pending_manifest(repo_case: tuple[Path, Path, dict[str, Any]]) -> None:
    root, bundle, payload = repo_case
    assert validate_candidate(payload, root, bundle) is None


@pytest.mark.parametrize("version", [4, None])
def test_manifest_dispatch_preserves_v2_schema_error_for_other_versions(
    repo_case: tuple[Path, Path, dict[str, Any]],
    version: int | None,
) -> None:
    root, bundle, payload = repo_case
    if version is None:
        payload.pop("schema_version")
    else:
        payload["schema_version"] = version
    with pytest.raises(ManifestError, match="schema_version"):
        validate_candidate(payload, root, bundle)


def _make_v3_pending_case(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> tuple[Path, Path, dict[str, Any]]:
    root, bundle, payload = repo_case
    payload["schema_version"] = 3
    python_components = sorted(
        (row for row in payload["candidate"]["components"] if row["kind"] == "python_distribution"),
        key=lambda row: row["id"],
    )
    payload["support_profile"]["python_dependency_selection"] = (
        release_manifest._canonical_v3_python_dependency_selection(
            [row["id"] for row in python_components]
        )
    )
    console_id = "@module-strategy/market-data-console"
    console_artifact = next(
        row for row in payload["candidate"]["artifacts"] if row["component"] == console_id
    )
    console_artifact["kind"] = "web_assets"
    zip_path = bundle / "dist/console-v3.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_member = b"<!doctype html><title>fixture</title>"
    with zipfile.ZipFile(zip_path, "w") as archive:
        info = zipfile.ZipInfo("index.html", (1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o100644 << 16
        archive.writestr(info, zip_member)
    console_artifact["file"] = _ref(bundle, zip_path, "bundle")
    for evidence in payload["evidence"]:
        if evidence["component_id"] == console_id:
            evidence["artifact_sha256"] = console_artifact["file"]["sha256"]
    tracked_inputs = release_contract._tracked_console_paths(root)
    members = [
        {
            "path": "index.html",
            "size": len(zip_member),
            "sha256": hashlib.sha256(zip_member).hexdigest(),
        }
    ]
    payload["candidate"]["web_console"] = {
        "component_id": console_id,
        "npm_lock_id": "console-runtime",
        "source_inputs": [_ref(root, root / path) for path in tracked_inputs],
        "artifact_sha256": console_artifact["file"]["sha256"],
        "artifact_members": members,
        "member_manifest_sha256": hashlib.sha256(
            json.dumps(
                members,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
    }
    pending = {"status": "pending", "value": None}
    payload["support_profile"]["web_console"] = {
        "toolchain": {
            key: dict(pending)
            for key in (
                "node_version",
                "npm_version",
                "provisioning_material",
                "registry_policy",
                "install_script_policy",
            )
        },
        "linux_build": {
            key: dict(pending)
            for key in ("architecture", "image", "recipe", "reconstruction_receipt")
        },
        "linux_serving": {key: dict(pending) for key in ("runtime", "configuration")},
        "linux_deployment": {key: dict(pending) for key in ("context", "evidence")},
        "darwin_acceptance": {
            key: dict(pending)
            for key in ("context", "browser_contexts", "artifact_use", "evidence")
        },
    }
    _refresh(payload, bundle)
    return root, bundle, payload


def test_v3_schema_and_pending_web_material_project_blocking_obligations(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    assert validate_candidate(payload, root, bundle) is None
    report = build_obligation_report(payload, root, bundle)
    assert report.manifest_schema_version == 3
    assert any(
        row.rule_id == "web_console.artifact_members" and row.status == "satisfied"
        for row in report.obligations
    )
    assert any(
        row.rule_id == "web_console.darwin_acceptance" and row.status == "unknown"
        for row in report.obligations
    )


def test_v3_dependency_selection_covers_all_providers_and_records_exact_functional_groups(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    _, _, payload = _make_v3_pending_case(repo_case)
    selection = payload["support_profile"]["python_dependency_selection"]
    assert selection["schema_version"] == 1
    assert len(selection["providers"]) == 26
    assert [row["component_id"] for row in selection["providers"]] == sorted(
        row["component_id"] for row in selection["providers"]
    )
    assert selection == release_manifest._canonical_v3_python_dependency_selection(
        [row["component_id"] for row in selection["providers"]]
    )
    expected_extras = {
        "modular-strategy-core": ("backtest", "signal-bot"),
        "modular-market-data-ingestion": ("interface", "parquet"),
        "modular-control-plane": ("interface",),
        "modular-system-acceptance": ("package-runtime-owner",),
    }
    assert release_manifest._V3_SELECTED_PYTHON_EXTRAS == expected_extras
    provider_ids = {row["component_id"] for row in selection["providers"]}
    fixture_expected_extras = {
        component_id: extras
        for component_id, extras in expected_extras.items()
        if component_id in provider_ids
    }
    assert {
        row["component_id"]: tuple(row["extras"]) for row in selection["providers"] if row["extras"]
    } == fixture_expected_extras
    assert all(
        row["extras"] == []
        for row in selection["providers"]
        if row["component_id"] not in fixture_expected_extras
    )
    assert all(
        lock.get("python_runtime_resolution_receipt")
        for lock in payload["candidate"]["locks"]
        if lock["ecosystem"] == "python" and lock["purpose"] == "runtime"
    )
    assert all(
        "python_runtime_resolution_receipt" not in lock
        for lock in payload["candidate"]["locks"]
        if lock["ecosystem"] == "python" and lock["purpose"] in {"bootstrap", "build"}
    )


def test_v3_selected_extra_policy_matches_real_inventory_and_static_descriptors() -> None:
    repository = Path(__file__).resolve().parents[1]
    inventory = json.loads((repository / "profiles/component-inventory.json").read_text())
    providers = set(
        next(
            row["providers"]
            for row in inventory["roles"]
            if row["capability"] == "python_distributions"
        )
    )
    expected = {
        "modular-strategy-core": ("backtest", "signal-bot"),
        "modular-market-data-ingestion": ("interface", "parquet"),
        "modular-control-plane": ("interface",),
        "modular-system-acceptance": ("package-runtime-owner",),
    }
    assert release_manifest._V3_SELECTED_PYTHON_EXTRAS == expected
    assert set(expected) <= providers

    descriptor_paths = [
        repository / "pyproject.toml",
        *sorted((repository / "applications").glob("*/pyproject.toml")),
        *sorted((repository / "modules").glob("*/pyproject.toml")),
        *sorted((repository / "plugins").glob("*/pyproject.toml")),
        *sorted((repository / "research").glob("*/pyproject.toml")),
    ]
    descriptors: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in descriptor_paths:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        name = project.get("name")
        if isinstance(name, str):
            assert name not in descriptors
            descriptors[name] = (path, project)

    for component_id, groups in expected.items():
        assert component_id in descriptors
        path, project = descriptors[component_id]
        assert path.relative_to(repository).as_posix() in {
            "pyproject.toml",
            "modules/market_data_ingestion/pyproject.toml",
            "applications/control_plane/pyproject.toml",
            "applications/system_acceptance/pyproject.toml",
        }
        dynamic = project.get("dynamic", [])
        assert isinstance(dynamic, list)
        assert "optional-dependencies" not in dynamic
        optional = project.get("optional-dependencies")
        assert isinstance(optional, dict)
        for group in groups:
            requirements = optional.get(group)
            assert isinstance(requirements, list) and requirements
            for requirement_text in requirements:
                assert isinstance(requirement_text, str)
                requirement = Requirement(requirement_text)
                assert requirement.url is None
                assert not requirement.extras


def test_canonical_selection_rows_are_sorted_and_cover_all_real_providers() -> None:
    repository = Path(__file__).resolve().parents[1]
    inventory = json.loads((repository / "profiles/component-inventory.json").read_text())
    providers = sorted(
        next(
            row["providers"]
            for row in inventory["roles"]
            if row["capability"] == "python_distributions"
        )
    )
    selection = release_manifest._canonical_v3_python_dependency_selection(
        list(reversed(providers))
    )
    expected_extras = release_manifest._V3_SELECTED_PYTHON_EXTRAS
    assert len(providers) == 26
    assert [row["component_id"] for row in selection["providers"]] == providers
    assert {
        row["component_id"]: tuple(row["extras"]) for row in selection["providers"] if row["extras"]
    } == expected_extras
    assert all(
        row["extras"] == []
        for row in selection["providers"]
        if row["component_id"] not in expected_extras
    )


def test_selection_fragment_is_source_bound_and_labeled_as_fragment(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    repository, _, _ = repo_case
    source_commit = release_manifest._git(repository, "rev-parse", "HEAD")
    fragment = build_profile_selection_fragment(repository, source_commit)
    assert fragment["artifact_type"] == "source_bound_profile_selection_fragment"
    assert fragment["source"] == {
        "repository": release_manifest.CANONICAL_REPOSITORY,
        "commit": source_commit,
    }
    assert fragment["python_dependency_selection"]["schema_version"] == 1
    assert len(fragment["python_dependency_selection"]["providers"]) == 26
    assert "not a resolver input, lock, receipt, or acceptance" in fragment["usage"]


def test_dependency_input_cli_fragment_mode_and_default_report_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fragment = {"artifact_type": "source_bound_profile_selection_fragment"}
    report = {"schema_version": 1, "resolution_status": {"state": "declared_inputs_only"}}
    monkeypatch.setattr(dependency_inputs, "_git", lambda *_args: "a" * 40)
    monkeypatch.setattr(
        dependency_inputs, "build_profile_selection_fragment", lambda *_args: fragment
    )
    monkeypatch.setattr(dependency_inputs, "build_dependency_input_report", lambda *_args: report)

    assert dependency_inputs.main(["--source-root", ".", "--selection-fragment"]) == 0
    fragment_output = capsys.readouterr().out
    assert json.loads(fragment_output) == fragment

    assert dependency_inputs.main(["--source-root", "."]) == 0
    report_output = capsys.readouterr().out
    assert json.loads(report_output) == report
    assert "artifact_type" not in json.loads(report_output)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "too short"),
        ("provider", "Python dependency selection must contain one canonical row"),
        ("tooling-extra", "unsupported extras"),
    ],
)
def test_v3_dependency_selection_rejects_missing_providers_and_unselected_groups(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str, message: str
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    providers = payload["support_profile"]["python_dependency_selection"]["providers"]
    if mutation == "missing":
        providers.pop()
    elif mutation == "provider":
        providers[-1]["component_id"] = "not-a-provider"
        providers.sort(key=lambda row: row["component_id"])
    else:
        providers[0]["extras"] = ["dev"]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_v3_runtime_resolution_receipt_binds_effective_input_and_v2_static_extra_rule_stays(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    runtime_lock = next(
        row for row in payload["candidate"]["locks"] if row["id"] == "python-runtime-312"
    )
    receipt_path = bundle / runtime_lock["python_runtime_resolution_receipt"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    input_path = bundle / receipt["resolver_input"]["path"]
    input_path.write_bytes(b"{}\n")
    receipt["resolver_input"] = _ref(bundle, input_path, "bundle")
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt_path.write_bytes(raw)
    runtime_lock["python_runtime_resolution_receipt"]["sha256"] = sha256_bytes(raw)
    _refresh(payload, bundle, refresh_runtime_receipts=False)
    with pytest.raises(ManifestError, match="effective input bytes mismatch"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_commit", "f" * 40, "identity mismatch"),
        ("selection_sha256", "f" * 64, "selection digest mismatch"),
        ("source_descriptors", [], "descriptor binding mismatch"),
        ("target_environment", "other-target", "identity mismatch"),
        ("lock_sha256", "f" * 64, "identity mismatch"),
        ("outcome", "failed", "identity mismatch"),
        ("run_id", "", "run identity is invalid"),
        ("attempt", 0, "run identity is invalid"),
    ],
)
def test_v3_runtime_receipt_rejects_binding_mismatch(
    repo_case: tuple[Path, Path, dict[str, Any]], field: str, value: Any, message: str
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    lock = next(row for row in payload["candidate"]["locks"] if row["id"] == "python-runtime-312")
    reference = lock["python_runtime_resolution_receipt"]
    receipt_path = bundle / reference["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = value
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt_path.write_bytes(raw)
    reference["sha256"] = sha256_bytes(raw)
    _refresh(payload, bundle, refresh_runtime_receipts=False)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_v3_runtime_receipts_are_required_only_for_runtime_python_locks(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    runtime_lock = next(
        row for row in payload["candidate"]["locks"] if row["id"] == "python-runtime-312"
    )
    del runtime_lock["python_runtime_resolution_receipt"]
    with pytest.raises(ManifestError, match="python_runtime_resolution_receipt"):
        validate_candidate(payload, root, bundle)


def test_v3_runtime_receipt_input_and_policy_refs_reach_validated_facts(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    captured: list[Any] = []
    original = release_report_module._report_from_validated_facts

    def capture(facts: Any) -> Any:
        captured.append(facts)
        return original(facts)

    monkeypatch.setattr(release_report_module, "_report_from_validated_facts", capture)
    build_obligation_report(payload, root, bundle)
    references = {item.role: item for item in captured[0].verified_references}
    for lock_id in ("python-runtime-312", "python-runtime-313"):
        lock = next(row for row in payload["candidate"]["locks"] if row["id"] == lock_id)
        receipt_path = lock["python_runtime_resolution_receipt"]["path"]
        receipt = json.loads((bundle / receipt_path).read_text(encoding="utf-8"))
        assert (
            references[f"runtime_resolution:{lock_id}:receipt"].sha256
            == lock["python_runtime_resolution_receipt"]["sha256"]
        )
        assert (
            references[f"runtime_resolution:{lock_id}:input"].sha256
            == receipt["resolver_input"]["sha256"]
        )
        assert (
            references[f"runtime_resolution:{lock_id}:policy"].sha256
            == receipt["resolver_index_policy"]["sha256"]
        )


@pytest.mark.parametrize(
    ("field", "malformation"),
    [
        ("resolver_input", "not-an-object"),
        ("resolver_input", "extra-field"),
        ("resolver_index_policy", "not-an-object"),
        ("resolver_index_policy", "extra-field"),
    ],
)
def test_v3_runtime_receipt_nested_references_fail_as_manifest_errors(
    repo_case: tuple[Path, Path, dict[str, Any]], field: str, malformation: str
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    lock = next(row for row in payload["candidate"]["locks"] if row["id"] == "python-runtime-312")
    lock_ref = lock["python_runtime_resolution_receipt"]
    receipt_path = bundle / lock_ref["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if malformation == "not-an-object":
        receipt[field] = "bad-reference"
    else:
        receipt[field]["unexpected"] = "extra"
    raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt_path.write_bytes(raw)
    lock_ref["sha256"] = sha256_bytes(raw)
    _refresh(payload, bundle, refresh_runtime_receipts=False)
    with pytest.raises(ManifestError, match="exactly origin, path, and sha256"):
        validate_candidate(payload, root, bundle)


def test_v3_selected_runtime_requirements_are_covered_without_changing_build_or_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path,
        python_optional_dependencies={0: {"research": ["fixture-extra>=1.0"]}},
    )
    root, bundle, payload = _make_v3_pending_case((root, bundle, payload))
    monkeypatch.setattr(
        release_manifest,
        "_V3_SELECTED_PYTHON_EXTRAS",
        {"fixture-distribution-00": ("research",)},
    )
    providers = payload["support_profile"]["python_dependency_selection"]["providers"]
    providers[0]["extras"] = ["research"]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="omits source-declared dependency pin: fixture-extra"):
        validate_candidate(payload, root, bundle)

    for lock_id in ("python-runtime-312", "python-runtime-313"):
        _replace_lock(
            bundle,
            payload,
            lock_id,
            _requirements_lock(("fixture-dependency", "1.0.0"), ("fixture-extra", "1.0.0")),
        )
    assert validate_candidate(payload, root, bundle) is None


def test_v3_signal_bot_extra_enters_only_effective_runtime_lock_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Path(__file__).resolve().parents[1]
    source_project = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    optional = source_project["optional-dependencies"]
    selected_groups = list(release_manifest._V3_SELECTED_PYTHON_EXTRAS["modular-strategy-core"])
    assert selected_groups == ["backtest", "signal-bot"]
    assert optional["signal-bot"] == ["matplotlib>=3.8"]

    tooling_groups = ("dev", "security", "release-validation")
    fixture_groups = {group: list(optional[group]) for group in (*selected_groups, *tooling_groups)}
    root, bundle, payload = _make_repo_case(
        tmp_path,
        python_optional_dependencies={0: fixture_groups},
    )
    root, bundle, payload = _make_v3_pending_case((root, bundle, payload))
    monkeypatch.setattr(
        release_manifest,
        "_V3_SELECTED_PYTHON_EXTRAS",
        {"fixture-distribution-00": tuple(selected_groups)},
    )
    providers = payload["support_profile"]["python_dependency_selection"]["providers"]
    providers[0]["extras"] = selected_groups
    _refresh(payload, bundle)

    for runtime_lock_id in ("python-runtime-312", "python-runtime-313"):
        _replace_lock(
            bundle,
            payload,
            runtime_lock_id,
            _requirements_lock(
                ("fixture-dependency", "1.0.0"),
                ("matplotlib", "3.8.0"),
                ("pyarrow", "16.0.0"),
            ),
        )

    assert validate_candidate(payload, root, bundle) is None
    expected_selected = {
        requirement for group in selected_groups for requirement in optional[group]
    }
    unselected_tooling = {
        requirement for group in tooling_groups for requirement in optional[group]
    }
    for runtime_lock_id in ("python-runtime-312", "python-runtime-313"):
        lock = next(row for row in payload["candidate"]["locks"] if row["id"] == runtime_lock_id)
        receipt = json.loads(
            (bundle / lock["python_runtime_resolution_receipt"]["path"]).read_text()
        )
        effective_input = json.loads((bundle / receipt["resolver_input"]["path"]).read_text())
        runtime_requirements = {
            row["requirement"]
            for row in effective_input["requirements"]
            if row["component_id"] == "fixture-distribution-00"
        }
        assert expected_selected <= runtime_requirements
        assert not (unselected_tooling & runtime_requirements)

    for purpose in ("bootstrap", "build"):
        for minor in ("312", "313"):
            lock = next(
                row
                for row in payload["candidate"]["locks"]
                if row["id"] == f"python-{purpose}-{minor}"
            )
            lock_text = (bundle / lock["file"]["path"]).read_text(encoding="utf-8")
            assert "matplotlib==" not in lock_text
            assert "pyarrow==" not in lock_text


@pytest.mark.parametrize(
    ("descriptor_kwargs", "message"),
    [
        ({"dynamic_optional_dependencies": {0}}, "cannot be dynamic"),
        (
            {
                "python_optional_dependencies": {
                    0: {"research": ["fixture-extra @ https://example.invalid/x.whl"]}
                }
            },
            "direct URL requirements",
        ),
    ],
)
def test_v3_selected_dynamic_groups_and_unsupported_requirements_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_kwargs: dict[str, Any],
    message: str,
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, **descriptor_kwargs)
    root, bundle, payload = _make_v3_pending_case((root, bundle, payload))
    monkeypatch.setattr(
        release_manifest,
        "_V3_SELECTED_PYTHON_EXTRAS",
        {"fixture-distribution-00": ("research",)},
    )
    payload["support_profile"]["python_dependency_selection"]["providers"][0]["extras"] = [
        "research"
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_v3_selected_group_must_exist_in_the_static_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path)
    root, bundle, payload = _make_v3_pending_case((root, bundle, payload))
    monkeypatch.setattr(
        release_manifest,
        "_V3_SELECTED_PYTHON_EXTRAS",
        {"fixture-distribution-00": ("missing-group",)},
    )
    payload["support_profile"]["python_dependency_selection"]["providers"][0]["extras"] = [
        "missing-group"
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="selected optional dependency group is missing"):
        validate_candidate(payload, root, bundle)


def test_v3_bound_selections_are_validated_and_remain_unknown_until_verified(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    context = payload["support_profile"]["web_console"]["linux_deployment"]["context"]
    context["status"] = "bound"
    context["value"] = {
        "os": "linux",
        "environment_id": "different-env",
        "runtime_id": "linux-serving",
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="sole target environment"):
        validate_candidate(payload, root, bundle)

    context["value"]["environment_id"] = payload["support_profile"]["target_environments"][0]
    _refresh(payload, bundle)
    assert validate_candidate(payload, root, bundle) is None
    report = build_obligation_report(payload, root, bundle)
    assert any(
        row.rule_id == "web_console.linux_deployment" and row.status == "unknown"
        for row in report.obligations
    )


def test_v3_linux_deployment_and_darwin_acceptance_bindings_stay_unverified_and_separate(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    profile = payload["support_profile"]
    web = profile["web_console"]
    target_environment = profile["target_environments"][0]
    console_id = payload["candidate"]["web_console"]["component_id"]
    darwin_environment = "darwin-console-acceptance"

    web["linux_deployment"]["context"] = {
        "status": "bound",
        "value": {
            "os": "linux",
            "environment_id": target_environment,
            "runtime_id": "owner-selected-linux-serving-runtime",
        },
    }
    web["linux_serving"]["runtime"] = {
        "status": "bound",
        "value": {
            "environment_id": target_environment,
            "runtime_id": "owner-selected-linux-serving-runtime",
            "mode": "static_asset",
        },
    }
    web["darwin_acceptance"]["context"] = {
        "status": "bound",
        "value": {"os": "darwin", "environment_id": darwin_environment},
    }
    console_requirement = next(
        row for row in profile["acceptance_requirements"] if row["component_id"] == console_id
    )
    console_requirement["environment"] = darwin_environment
    console_evidence = next(
        row
        for row in payload["evidence"]
        if row["component_id"] == console_id and row["gate"] == console_requirement["gate"]
    )
    console_evidence["environment"] = darwin_environment
    _refresh(payload, bundle)

    assert validate_candidate(payload, root, bundle) is None
    report = build_obligation_report(payload, root, bundle)
    console_obligations = [
        row
        for row in report.obligations
        if row.rule_id
        in {
            "web_console.linux_serving",
            "web_console.linux_deployment",
            "web_console.darwin_acceptance",
        }
    ]
    assert {row.rule_id for row in console_obligations} == {
        "web_console.linux_serving",
        "web_console.linux_deployment",
        "web_console.darwin_acceptance",
    }
    assert all(row.status == "unknown" for row in console_obligations)
    assert profile["target_environments"] == [target_environment]
    assert all(
        row["target_environment"] == target_environment for row in payload["candidate"]["locks"]
    )
    assert darwin_environment not in profile["target_environments"]
    assert all(
        row["target_environment"] == target_environment for row in profile["lock_requirements"]
    )


@pytest.mark.parametrize(
    ("group_name", "value", "message"),
    [
        (
            "linux_deployment",
            {"os": "darwin", "environment_id": "owner-defined", "runtime_id": "linux-serving"},
            "os",
        ),
        (
            "darwin_acceptance",
            {"os": "linux", "environment_id": "darwin-console-acceptance"},
            "os",
        ),
    ],
)
def test_v3_context_rejects_os_environment_swaps(
    repo_case: tuple[Path, Path, dict[str, Any]],
    group_name: str,
    value: dict[str, str],
    message: str,
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    payload["support_profile"]["web_console"][group_name]["context"] = {
        "status": "bound",
        "value": value,
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_v3_serving_runtime_must_use_linux_deployment_target(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    web = payload["support_profile"]["web_console"]
    target_environment = payload["support_profile"]["target_environments"][0]
    web["linux_deployment"]["context"] = {
        "status": "bound",
        "value": {
            "os": "linux",
            "environment_id": target_environment,
            "runtime_id": "serving-runtime",
        },
    }
    web["linux_serving"]["runtime"] = {
        "status": "bound",
        "value": {
            "environment_id": "different-environment",
            "runtime_id": "serving-runtime",
            "mode": "static_asset",
        },
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="sole target environment"):
        validate_candidate(payload, root, bundle)


def test_v3_linux_context_must_reference_the_bound_serving_runtime(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    web = payload["support_profile"]["web_console"]
    target_environment = payload["support_profile"]["target_environments"][0]
    web["linux_deployment"]["context"] = {
        "status": "bound",
        "value": {
            "os": "linux",
            "environment_id": target_environment,
            "runtime_id": "context-runtime",
        },
    }
    web["linux_serving"]["runtime"] = {
        "status": "bound",
        "value": {
            "environment_id": target_environment,
            "runtime_id": "serving-runtime",
            "mode": "static_asset",
        },
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="different serving runtime"):
        validate_candidate(payload, root, bundle)


def test_v3_darwin_context_cannot_reuse_linux_deployment_environment(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    target_environment = payload["support_profile"]["target_environments"][0]
    payload["support_profile"]["web_console"]["darwin_acceptance"]["context"] = {
        "status": "bound",
        "value": {"os": "darwin", "environment_id": target_environment},
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="cannot become a deployment target"):
        validate_candidate(payload, root, bundle)


def test_v3_darwin_context_must_match_declared_console_acceptance_environment(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    payload["support_profile"]["web_console"]["darwin_acceptance"]["context"] = {
        "status": "bound",
        "value": {"os": "darwin", "environment_id": "undeclared-darwin"},
    }
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="declared Console acceptance environment"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("member_name", ["../escape", "a/../escape", "/absolute"])
def test_v3_zip_rejects_traversal_members(
    repo_case: tuple[Path, Path, dict[str, Any]], member_name: str
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    artifact = next(row for row in payload["candidate"]["artifacts"] if row["kind"] == "web_assets")
    archive_path = bundle / artifact["file"]["path"]
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(member_name, b"x")
    artifact["file"] = _ref(bundle, archive_path, "bundle")
    payload["candidate"]["web_console"]["artifact_sha256"] = artifact["file"]["sha256"]
    for evidence in payload["evidence"]:
        if evidence["component_id"] == artifact["component"]:
            evidence["artifact_sha256"] = artifact["file"]["sha256"]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="unsafe/nondeterministic"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    "layout", ["casefold", "duplicate", "prefix", "casefold_prefix", "symlink"]
)
def test_v3_zip_rejects_collisions_and_symlinks(
    repo_case: tuple[Path, Path, dict[str, Any]], layout: str
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    artifact = next(row for row in payload["candidate"]["artifacts"] if row["kind"] == "web_assets")
    archive_path = bundle / artifact["file"]["path"]
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    names = {
        "casefold": ["Index.html", "index.html"],
        "duplicate": ["index.html", "index.html"],
        "prefix": ["assets", "assets/app.js"],
        "casefold_prefix": ["A", "a/app.js"],
        "symlink": ["link"],
    }[layout]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name in names:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o120777 if layout == "symlink" else 0o100644) << 16
            archive.writestr(info, b"x")
    artifact["file"] = _ref(bundle, archive_path, "bundle")
    payload["candidate"]["web_console"]["artifact_sha256"] = artifact["file"]["sha256"]
    for evidence in payload["evidence"]:
        if evidence["component_id"] == artifact["component"]:
            evidence["artifact_sha256"] = artifact["file"]["sha256"]
    # A valid single declaration is sufficient: the hostile archive must be
    # rejected before it can be mistaken for a complete artifact.
    payload["candidate"]["web_console"]["artifact_members"] = [
        {"path": "index.html", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    ]
    payload["candidate"]["web_console"]["member_manifest_sha256"] = hashlib.sha256(
        json.dumps(
            payload["candidate"]["web_console"]["artifact_members"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="unsafe/nondeterministic|path conflict"):
        validate_candidate(payload, root, bundle)


def test_v3_zip_expansion_limit_blocks_archive(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    monkeypatch.setattr("scripts.platform_release_manifest.MAX_WEB_ASSET_EXPANDED_BYTES", 1)
    with pytest.raises(ManifestError, match="expanded size limit"):
        validate_candidate(payload, root, bundle)


def test_v3_zip_compressed_limit_is_enforced_during_read(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    artifact = next(row for row in payload["candidate"]["artifacts"] if row["kind"] == "web_assets")
    archive_path = bundle / artifact["file"]["path"]
    archive_path.write_bytes(b"larger-than-limit")
    artifact["file"] = _ref(bundle, archive_path, "bundle")
    payload["candidate"]["web_console"]["artifact_sha256"] = artifact["file"]["sha256"]
    for evidence in payload["evidence"]:
        if evidence["component_id"] == artifact["component"]:
            evidence["artifact_sha256"] = artifact["file"]["sha256"]
    monkeypatch.setattr("scripts.platform_release_manifest.MAX_WEB_ASSET_ARCHIVE_BYTES", 1)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="compressed-size limit"):
        validate_candidate(payload, root, bundle)


def test_v3_zip_member_projection_preserves_each_member(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = _make_v3_pending_case(repo_case)
    artifact = next(row for row in payload["candidate"]["artifacts"] if row["kind"] == "web_assets")
    archive_path = bundle / artifact["file"]["path"]
    contents = {"index.html": b"html", "assets/app.js": b"javascript"}
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, content in sorted(contents.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    artifact["file"] = _ref(bundle, archive_path, "bundle")
    payload["candidate"]["web_console"]["artifact_sha256"] = artifact["file"]["sha256"]
    members = [
        {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(contents.items())
    ]
    payload["candidate"]["web_console"]["artifact_members"] = members
    payload["candidate"]["web_console"]["member_manifest_sha256"] = hashlib.sha256(
        json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for evidence in payload["evidence"]:
        if evidence["component_id"] == artifact["component"]:
            evidence["artifact_sha256"] = artifact["file"]["sha256"]
    _refresh(payload, bundle)
    assert validate_candidate(payload, root, bundle) is None
    report = build_obligation_report(payload, root, bundle)
    member_claim = next(
        ref
        for obligation in report.obligations
        if obligation.rule_id == "web_console.artifact_members"
        for ref in obligation.matching_refs
        if ref.kind == "web_console_artifact_members"
    )
    assert member_claim.identity == "@module-strategy/market-data-console"


def test_pending_obligation_report_retains_blockers_and_required_scope(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    report = build_obligation_report(payload, root, bundle)

    console_id = "@module-strategy/market-data-console"
    assert any(
        row.rule_id == "compatibility.coverage"
        and row.scope.component_id == console_id
        and row.status == "unknown"
        for row in report.obligations
    )
    assert report.report_version == 1
    assert report.manifest_schema_version == 2
    assert report.identity.candidate_id == payload["candidate"]["candidate_id"]
    assert report.identity.profile_id == payload["support_profile"]["profile_id"]
    assert report.identity.candidate_digest == sha256_bytes(
        release_manifest.canonical_json_bytes(payload["candidate"])
    )
    assert report.identity.profile_digest == sha256_bytes(
        release_manifest.canonical_json_bytes(payload["support_profile"])
    )
    assert report.identity.candidate_digest != report.identity.candidate_id
    assert report.policy_bindings.manifest_schema_digest == sha256_bytes(
        (root / "schemas/platform-release-manifest-v2.schema.json").read_bytes()
    )
    assert (
        report.policy_bindings.component_inventory_digest
        == payload["support_profile"]["component_inventory"]["sha256"]
    )
    assert report.policy_bindings.compatibility_coverage_policy_digest == sha256_bytes(
        (root / "schemas/platform-compatibility-coverage-policy-v1.json").read_bytes()
    )
    assert (
        report.policy_bindings.acceptance_policy_digest
        == payload["support_profile"]["acceptance_policy"]["sha256"]
    )
    assert any(row.rule_id == "evidence.acceptance" for row in report.obligations)
    assert any(row.rule_id == "python.lock.applicability" for row in report.obligations)
    assert any(row.rule_id == "migration.evidence" for row in report.obligations)
    assert any(row.rule_id == "rollback.evidence" for row in report.obligations)
    assert any(
        row.rule_id == "python.build.context" and row.status == "missing"
        for row in report.obligations
    )
    assert any(
        row.rule_id == "python.build.attestation" and row.status == "missing"
        for row in report.obligations
    )
    acceptance_obligation = next(
        row for row in report.obligations if row.rule_id == "evidence.acceptance"
    )
    assert acceptance_obligation.required_refs and acceptance_obligation.matching_refs
    assert not hasattr(report, "accepted")
    assert not hasattr(report, "trusted")
    assert not hasattr(report, "release_ready")


def test_obligation_report_is_deeply_immutable_and_isolated_from_input_mutation(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    from dataclasses import FrozenInstanceError

    root, bundle, payload = repo_case
    report = build_obligation_report(payload, root, bundle)
    original_digest = report.manifest_digest
    original_obligations = report.obligations
    payload["candidate"]["components"][0]["id"] = "mutated-after-validation"
    payload["evidence"].clear()

    assert report.manifest_digest == original_digest
    assert report.obligations == original_obligations
    assert report.evidence_claims
    assert isinstance(report.obligations, tuple)
    with pytest.raises(FrozenInstanceError):
        report.manifest_digest = "f" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.obligations[0].status = "missing"  # type: ignore[misc]


def test_obligation_report_generation_is_deterministic(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    first = build_obligation_report(payload, root, bundle)
    second = build_obligation_report(payload, root, bundle)
    assert first == second


def test_validated_fact_aggregate_is_typed_immutable_and_keeps_verified_reference_identity(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import FrozenInstanceError

    root, bundle, payload = repo_case
    captured: list[Any] = []
    original = release_report_module._report_from_validated_facts

    def capture(facts: Any) -> Any:
        captured.append(facts)
        return original(facts)

    monkeypatch.setattr(release_report_module, "_report_from_validated_facts", capture)
    report = build_obligation_report(payload, root, bundle)
    facts = captured[0]

    assert facts.candidate.candidate_id == payload["candidate"]["candidate_id"]
    assert facts.candidate.profile_id == payload["support_profile"]["profile_id"]
    assert facts.components and facts.lock_requirements and facts.acceptance_requirements
    assert facts.compatibility_scopes and facts.evidence and facts.artifacts
    assert not hasattr(facts, "manifest")
    assert facts.components[0].descriptor_reference.origin == "source"
    assert facts.components[0].source_references
    lock_row = payload["candidate"]["locks"][0]
    lock_fact = next(item for item in facts.locks if item.lock_id == lock_row["id"])
    assert (lock_fact.reference.origin, lock_fact.reference.path, lock_fact.reference.sha256) == (
        lock_row["file"]["origin"],
        lock_row["file"]["path"],
        lock_row["file"]["sha256"],
    )
    assert isinstance(facts.verified_references, tuple)
    schema_ref = next(ref for ref in facts.verified_references if ref.role == "manifest_schema")
    artifact = payload["candidate"]["artifacts"][0]
    artifact_ref = next(
        ref
        for ref in facts.verified_references
        if ref.role == f"artifact:{artifact['component']}:{artifact['file']['path']}"
    )
    assert (schema_ref.origin, schema_ref.path, schema_ref.sha256) == (
        "source",
        "schemas/platform-release-manifest-v2.schema.json",
        facts.policies.schema_digest,
    )
    assert (artifact_ref.origin, artifact_ref.path, artifact_ref.sha256) == (
        artifact["file"]["origin"],
        artifact["file"]["path"],
        artifact["file"]["sha256"],
    )
    with pytest.raises(FrozenInstanceError):
        facts.candidate.candidate_id = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        facts.verified_references[0].sha256 = "mutated"  # type: ignore[misc]
    assert report.identity.candidate_id == facts.candidate.candidate_id
    original_candidate_id = facts.candidate.candidate_id
    payload["candidate"]["candidate_id"] = "mutated-after-freeze"
    assert facts.candidate.candidate_id == original_candidate_id


def test_obligation_report_distinguishes_missing_acceptance_match(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    requirement = payload["support_profile"]["acceptance_requirements"][0]
    payload["evidence"] = [
        row
        for row in payload["evidence"]
        if not (
            row["gate"] == requirement["gate"]
            and row["component_id"] == requirement["component_id"]
            and row["environment"] == requirement["environment"]
        )
    ]
    _refresh(payload, bundle)
    report = build_obligation_report(payload, root, bundle)
    missing = next(
        row
        for row in report.obligations
        if row.rule_id == "evidence.acceptance"
        and row.scope.gate_id == requirement["gate"]
        and row.scope.component_id == requirement["component_id"]
    )
    assert missing.status == "missing"
    assert missing.required_refs
    assert not missing.matching_refs


def test_report_preserves_confirmed_console_unknown_runtime_and_subject_match(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    console_id = "@module-strategy/market-data-console"
    console_coverage = next(
        row
        for row in payload["candidate"]["compatibility_coverage"]
        if row["component_id"] == console_id
    )
    _attach_compatibility_subject_evidence(
        payload,
        {
            **console_coverage,
            "status": "confirmed",
            "inventory_status": "none",
            "owner": "fixture owner confirmation",
            "formats": [],
        },
    )
    python_coverage = _confirmed_compatibility_row(payload)
    _attach_compatibility_subject_evidence(payload, python_coverage)
    _refresh(payload, bundle)

    report = build_obligation_report(payload, root, bundle)
    console_runtime = next(
        row
        for row in report.obligations
        if row.rule_id == "compatibility.runtime_scope" and row.scope.component_id == console_id
    )
    assert console_runtime.status == "unknown"
    assert console_runtime.scope.runtime_id is None
    assert console_runtime.matching_refs

    subject_claims = [
        row for row in report.evidence_claims if row.compatibility_subject_sha256 is not None
    ]
    assert any(row.component_id == console_id and row.runtime_id is None for row in subject_claims)
    assert any(row.runtime_id in {"cpython-3.12.8", "cpython-3.13.1"} for row in subject_claims)
    python_runtime_obligations = [
        row
        for row in report.obligations
        if row.rule_id == "compatibility.runtime_scope"
        and row.scope.component_id == python_coverage["component_id"]
    ]
    assert python_runtime_obligations
    assert all(
        row.status == "satisfied" and row.matching_refs for row in python_runtime_obligations
    )


def test_unknown_compatibility_coverage_does_not_project_unvalidated_relations(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    component_id = row["component_id"]
    row["status"] = "unknown-blocking"
    row["formats"][0]["relations"][0]["target"]["component_id"] = "inconsistent-candidate-fact"
    payload["candidate"]["compatibility_coverage"] = [
        row if existing["component_id"] == component_id else existing
        for existing in payload["candidate"]["compatibility_coverage"]
    ]
    _refresh(payload, bundle)
    # Unknown coverage is intentionally skipped after schema validation in v2.
    # Bypass only the schema checkpoint so an inconsistent but well-shaped
    # relation can exercise that existing validator branch and report boundary.
    _skip_optional_jsonschema_dependency(monkeypatch)

    report = build_obligation_report(payload, root, bundle)
    assert any(
        obligation.rule_id == "compatibility.coverage"
        and obligation.scope.component_id == component_id
        and obligation.status == "unknown"
        for obligation in report.obligations
    )
    assert any(
        obligation.rule_id == "compatibility.predecessor"
        and obligation.scope.component_id == component_id
        and obligation.status == "unknown"
        and not obligation.scope.relation_id
        for obligation in report.obligations
    )
    assert not any(
        obligation.scope.component_id == component_id
        and obligation.scope.relation_id
        and obligation.rule_id
        in {"compatibility.predecessor", "compatibility.relation_declaration"}
        for obligation in report.obligations
    )


def test_confirmed_compatibility_relation_does_not_resolve_predecessor_material(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    _attach_compatibility_subject_evidence(payload, row)
    payload["candidate"]["compatibility_coverage"] = [
        row if existing["component_id"] == row["component_id"] else existing
        for existing in payload["candidate"]["compatibility_coverage"]
    ]
    _refresh(payload, bundle)

    report = build_obligation_report(payload, root, bundle)
    declaration = next(
        obligation
        for obligation in report.obligations
        if obligation.rule_id == "compatibility.relation_declaration"
        and obligation.scope.component_id == row["component_id"]
    )
    predecessor = next(
        obligation
        for obligation in report.obligations
        if obligation.rule_id == "compatibility.predecessor"
        and obligation.scope.component_id == row["component_id"]
        and obligation.scope.relation_id
    )
    assert declaration.status == "satisfied" and declaration.matching_refs
    assert predecessor.status == "unknown"
    assert predecessor.required_refs and not predecessor.matching_refs


def test_report_carries_reconstruction_and_typed_build_bindings(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    payload["release_decision"]["decision"] = "pending"
    payload["release_decision"]["owner_approval"]["decision"] = "pending"
    _refresh(payload, bundle)

    report = build_obligation_report(payload, root, bundle)
    assert report.reconstructions
    assert all(
        row.receipt_sha256 and row.lock_sha256 and row.outcome == "passed"
        for row in report.reconstructions
    )
    assert report.build_contexts and report.hook_bindings and report.build_bindings
    assert all(row.runtime_id for row in report.build_contexts)
    assert all(
        row.final_lock_sha256 and row.reconstruction_receipt_sha256 for row in report.hook_bindings
    )
    assert all(row.runtime_id and row.artifact_sha256 for row in report.build_bindings)
    assert any(
        row.rule_id == "python.build.attestation" and row.status == "satisfied"
        for row in report.obligations
    )


def test_report_preserves_all_same_scope_build_contexts_deterministically(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    payload["release_decision"]["decision"] = "pending"
    payload["release_decision"]["owner_approval"]["decision"] = "pending"
    _refresh(payload, bundle)

    original = payload["build_contexts"][0]
    duplicate = copy.deepcopy(original)
    duplicate["config_settings"] = {"fixture_variant": "second-context"}
    duplicate["context_sha256"] = "0" * 64
    duplicate["context_sha256"] = object_identity(duplicate, "context_sha256")
    prior_context_digest = original["context_sha256"]
    new_context_digest = duplicate["context_sha256"]
    cloned_hooks = []
    for hook in payload["hook_attestations"]:
        if hook["context_sha256"] != prior_context_digest:
            continue
        cloned = copy.deepcopy(hook)
        cloned["context_sha256"] = new_context_digest
        cloned["run_identity"] += "-second-context"
        cloned["attestation_sha256"] = "0" * 64
        cloned["attestation_sha256"] = object_identity(cloned, "attestation_sha256")
        cloned_hooks.append(cloned)
    payload["build_contexts"].append(duplicate)
    payload["hook_attestations"].extend(cloned_hooks)
    _refresh(payload, bundle)

    first = build_obligation_report(payload, root, bundle)
    matching_contexts = [
        row
        for row in first.build_contexts
        if (row.component_id, row.runtime_id, row.environment_id)
        == (
            original["component_id"],
            original["python_runtime_id"],
            "owner-defined",
        )
    ]
    context_ids = {str(row.context_sha256) for row in matching_contexts}
    assert len(context_ids) == 2
    matching_hooks = [row for row in first.hook_bindings if str(row.context_sha256) in context_ids]
    assert len(matching_hooks) == 4
    assert {str(row.context_sha256) for row in matching_hooks} == context_ids
    for context_id in context_ids:
        operations = {
            row.operation for row in matching_hooks if str(row.context_sha256) == context_id
        }
        assert operations == {"wheel", "sdist"}
    attestation_rows = [
        row
        for row in first.obligations
        if row.rule_id == "python.build.attestation"
        and row.scope.component_id == original["component_id"]
        and row.scope.runtime_id == original["python_runtime_id"]
        and row.scope.environment_id == "owner-defined"
    ]
    assert len(attestation_rows) == 4
    assert sum(row.status == "satisfied" for row in attestation_rows) == 2
    assert sum(row.status == "missing" for row in attestation_rows) == 2

    payload["build_contexts"].reverse()
    _refresh(payload, bundle)
    reversed_report = build_obligation_report(payload, root, bundle)
    assert first.obligations == reversed_report.obligations
    assert first.build_contexts == reversed_report.build_contexts
    assert first.hook_bindings == reversed_report.hook_bindings
    assert first.build_bindings == reversed_report.build_bindings


def test_obligation_report_preserves_v2_checkpoint_precedence_and_accepted_refusal(
    tmp_path: Path,
) -> None:
    precedence_dir = tmp_path / "precedence"
    precedence_dir.mkdir()
    root, bundle, payload = _make_repo_case(precedence_dir)
    payload["schema_version"] = 4
    payload["candidate"]["source"]["commit"] = "f" * 40
    with pytest.raises(ManifestError) as direct_error:
        validate_candidate(payload, root, bundle)
    with pytest.raises(ManifestError) as report_error:
        build_obligation_report(payload, root, bundle)
    assert str(direct_error.value) == str(report_error.value)
    assert "schema_version" in str(report_error.value)

    accepted_dir = tmp_path / "accepted"
    accepted_dir.mkdir()
    root, bundle, payload = _make_repo_case(accepted_dir)
    _accepted(payload, bundle)
    with pytest.raises(ManifestError) as direct_error:
        validate_candidate(payload, root, bundle)
    with pytest.raises(ManifestError) as report_error:
        build_obligation_report(payload, root, bundle)
    assert str(direct_error.value) == str(report_error.value)
    assert "accepted decision" in str(report_error.value)


def _commit_fixture_file(root: Path, relative_path: str, content: str) -> str:
    _write(root / relative_path, content)
    subprocess.run(["git", "-C", str(root), "add", relative_path], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture source update"], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_descriptor_changes(
    root: Path,
    bundle: Path,
    payload: dict[str, Any],
    replacements: dict[str, str],
) -> None:
    commit = payload["candidate"]["source"]["commit"]
    for path, content in replacements.items():
        commit = _commit_fixture_file(root, path, content)
        component = next(
            item
            for item in payload["candidate"]["components"]
            if item["descriptor"]["path"] == path
        )
        component["descriptor"]["sha256"] = sha256_bytes((root / path).read_bytes())
    payload["candidate"]["source"]["commit"] = commit
    _refresh(payload, bundle)


def _requirements_lock(*rows: tuple[str, str]) -> bytes:
    digest = "a" * 64
    return "".join(f"{name}=={version} --hash=sha256:{digest}\n" for name, version in rows).encode()


def _replace_purpose_locks(
    bundle: Path, payload: dict[str, Any], purpose: str, content: bytes
) -> None:
    for lock in payload["candidate"]["locks"]:
        if lock["ecosystem"] == "python" and lock["purpose"] in (
            {"build", "bootstrap"} if purpose == "build" else {purpose}
        ):
            _replace_lock(bundle, payload, lock["id"], content)


def _skip_optional_jsonschema_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    # These focused checks exercise source/lock policy after schema validation;
    # jsonschema is an optional extra and is absent from the isolated test host.
    monkeypatch.setattr(release_manifest, "_schema_validate", lambda *args, **kwargs: "0" * 64)
    monkeypatch.setattr(
        release_manifest,
        "_load_component_inventory",
        lambda reference, source, bundle, tracked, commit, *args, **kwargs: (
            json.loads((source / reference["path"]).read_text(encoding="utf-8"))["roles"],
            "1" * 64,
        ),
    )


def _add_typed_build_chain(
    bundle: Path, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    pin_content = (
        f"fixture-dependency==1.0.0 --hash=sha256:{'a' * 64}\n"
        f"fixture-backend==1.0.0 --hash=sha256:{'b' * 64}\n"
        f"fixture-frontend==1.0.0 --hash=sha256:{'c' * 64}\n"
    ).encode()
    for lock_id in (
        "python-bootstrap-312",
        "python-bootstrap-313",
        "python-build-312",
        "python-build-313",
    ):
        _replace_lock(bundle, payload, lock_id, pin_content)
    commit = payload["candidate"]["source"]["commit"]
    component = next(
        item for item in payload["candidate"]["components"] if item["kind"] == "python_distribution"
    )
    bootstrap_receipt = next(
        row for row in payload["lock_reconstructions"] if row["lock_id"] == "python-bootstrap-312"
    )
    build_receipt = next(
        row for row in payload["lock_reconstructions"] if row["lock_id"] == "python-build-312"
    )
    context: dict[str, Any] = {
        "schema_version": 1,
        "context_sha256": "0" * 64,
        "source_commit": commit,
        "component_id": component["id"],
        "component_version": component["version"],
        "descriptor": component["descriptor"],
        "backend": {
            "entrypoint": "fixture.backend",
            "distribution": "fixture-backend",
            "version": "1.0.0",
            "artifact_sha256": "b" * 64,
        },
        "backend_path": [],
        "python_runtime_id": "cpython-3.12.8",
        "target": {
            "environment": "owner-defined",
            "os": "fixture-os",
            "architecture": "fixture-arch",
            "abi": "fixture-abi",
        },
        "config_settings": {},
        "frontend": {"name": "fixture-frontend", "version": "1.0.0"},
        "toolchain": [
            {
                "name": "fixture-frontend",
                "version": "1.0.0",
                "artifact_sha256": "c" * 64,
            }
        ],
        "environment": {"identity": "owner-defined", "isolation": "isolated"},
        "resolver_policy": "fixture-policy",
        "bootstrap_lock_id": "python-bootstrap-312",
        "bootstrap_lock_sha256": next(
            row for row in payload["candidate"]["locks"] if row["id"] == "python-bootstrap-312"
        )["file"]["sha256"],
        "bootstrap_reconstruction_receipt_sha256": bootstrap_receipt["receipt"]["sha256"],
    }
    context["context_sha256"] = object_identity(context, "context_sha256")
    hook: dict[str, Any] = {
        "schema_version": 1,
        "attestation_sha256": "0" * 64,
        "context_sha256": context["context_sha256"],
        "operation": "wheel",
        "status": "returned",
        "returned_requirements": [],
        "final_lock_id": "python-build-312",
        "final_lock_sha256": next(
            row for row in payload["candidate"]["locks"] if row["id"] == "python-build-312"
        )["file"]["sha256"],
        "reconstruction_receipt_sha256": build_receipt["receipt"]["sha256"],
        "run_identity": "fixture-hook-run",
    }
    hook["attestation_sha256"] = object_identity(hook, "attestation_sha256")
    artifact = next(
        row
        for row in payload["candidate"]["artifacts"]
        if row["component"] == component["id"] and row["file"]["path"].endswith(".whl")
    )
    build: dict[str, Any] = {
        "schema_version": 1,
        "attestation_sha256": "0" * 64,
        "context_sha256": context["context_sha256"],
        "hook_attestation_sha256": hook["attestation_sha256"],
        "operation": "wheel",
        "python_runtime_id": "cpython-3.12.8",
        "target_environment": "owner-defined",
        "final_lock_id": hook["final_lock_id"],
        "final_lock_sha256": hook["final_lock_sha256"],
        "reconstruction_receipt_sha256": hook["reconstruction_receipt_sha256"],
        "outcome": "passed",
        "run_identity": "fixture-build-run",
        "artifact": dict(artifact["file"]),
    }
    build["attestation_sha256"] = object_identity(build, "attestation_sha256")
    payload["build_contexts"] = [context]
    payload["hook_attestations"] = [hook]
    payload["build_attestations"] = [build]
    return context, hook, build


def test_dependency_input_report_is_deterministic_source_bound_and_covers_inventory(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    commit = payload["candidate"]["source"]["commit"]

    report = build_dependency_input_report(root, commit)
    repeated = build_dependency_input_report(root, commit)

    assert release_manifest.canonical_json_bytes(report) == release_manifest.canonical_json_bytes(
        repeated
    )
    assert report["source"]["commit"] == commit
    assert report["python_distribution_count"] == 26
    assert len(report["python_distributions"]) == 26
    assert report["resolution_status"] == {
        "state": "declared_inputs_only",
        "resolved_locks": False,
        "python_312_313_reconstruction_proven": False,
    }
    assert report["npm_project"]["package"]["path"] == "web/market-data-console/package.json"
    assert report["npm_project"]["lock"]["path"] == "web/market-data-console/package-lock.json"
    npm_package = json.loads(
        (root / report["npm_project"]["package"]["path"]).read_text(encoding="utf-8")
    )
    for field in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "peerDependenciesMeta",
        "engines",
        "os",
        "cpu",
        "libc",
        "packageManager",
    ):
        metadata = report["npm_project"]["metadata"][field]
        assert metadata["state"] == ("static" if field in npm_package else "absent")
        if field in npm_package:
            assert metadata["value"] == npm_package[field]
    for reference_key in ("package", "lock"):
        reference = report["npm_project"][reference_key]
        assert reference["sha256"] == sha256_bytes((root / reference["path"]).read_bytes())
    validate_dependency_input_report(report, root)

    first = report["python_distributions"][0]
    assert first["descriptor"]["sha256"] == sha256_bytes(
        (root / first["descriptor"]["path"]).read_bytes()
    )
    assert first["build_system"]["state"] == "declared"
    assert first["dependencies"] == {"state": "static", "value": []}
    assert first["optional_dependencies"]["state"] == "absent"
    assert report["npm_project"]["metadata"]["engines"]["state"] == "static"


def test_dependency_inputs_preserve_ranges_markers_duplicates_and_optional_groups(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (root / descriptor).read_text(encoding="utf-8")
    content = content.replace("dependencies = []\n", "")
    content = content.replace(
        '[build-system]\nrequires = []\nbuild-backend = "fixture.backend"\n',
        "",
    )
    content += (
        "dependencies = [\"sample>=1,<3; python_version >= '3.12'\", "
        '"sample>=1,<3; python_version >= \'3.12\'", "other[extra]>=4"]\n'
        "[project.optional-dependencies]\n"
        "dev = [\"tool>=2; platform_system == 'Linux'\", "
        "\"tool>=2; platform_system == 'Linux'\"]\n"
        'docs = ["sphinx~=8.0"]\n'
        "[build-system]\n"
        'requires = ["backend>=1,<2", "backend>=1,<2"]\n'
        'build-backend = "backend.api"\n'
        'backend-path = ["build_backend"]\n'
    )
    commit = _commit_fixture_file(root, descriptor, content)

    report = build_dependency_input_report(root, commit)
    first = next(
        row for row in report["python_distributions"] if row["name"] == "fixture-distribution-00"
    )

    assert first["dependencies"] == {
        "state": "static",
        "value": [
            "sample>=1,<3; python_version >= '3.12'",
            "sample>=1,<3; python_version >= '3.12'",
            "other[extra]>=4",
        ],
    }
    assert first["optional_dependencies"] == {
        "state": "static",
        "groups": [
            {
                "name": "dev",
                "requirements": [
                    "tool>=2; platform_system == 'Linux'",
                    "tool>=2; platform_system == 'Linux'",
                ],
            },
            {"name": "docs", "requirements": ["sphinx~=8.0"]},
        ],
    }
    assert first["build_system"] == {
        "state": "declared",
        "value": {
            "requires": ["backend>=1,<2", "backend>=1,<2"],
            "build-backend": "backend.api",
            "backend-path": ["build_backend"],
        },
    }


def test_dependency_input_report_marks_dynamic_fields_incomplete(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (root / descriptor).read_text(encoding="utf-8")
    content = content.replace(
        "[build-system]",
        'dynamic = ["dependencies", "optional-dependencies"]\n[build-system]',
    )
    commit = _commit_fixture_file(root, descriptor, content)

    report = build_dependency_input_report(root, commit)
    first = next(
        row for row in report["python_distributions"] if row["name"] == "fixture-distribution-00"
    )

    assert first["dependencies"] == {
        "state": "unsupported_dynamic",
        "dynamic_provider_required": True,
    }
    assert first["optional_dependencies"] == {
        "state": "unsupported_dynamic",
        "dynamic_provider_required": True,
    }
    assert first["unsupported_dynamic_fields"] == ["dependencies", "optional-dependencies"]


def test_dependency_input_report_preserves_dynamic_requires_python_as_unsupported(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (root / descriptor).read_text(encoding="utf-8")
    content = content.replace('requires-python = ">=3.12"\n', "")
    content = content.replace("[build-system]", 'dynamic = ["requires-python"]\n[build-system]')
    commit = _commit_fixture_file(root, descriptor, content)

    report = build_dependency_input_report(root, commit)
    first = next(
        row for row in report["python_distributions"] if row["name"] == "fixture-distribution-00"
    )

    assert first["requires_python"] == {
        "state": "unsupported_dynamic",
        "dynamic_provider_required": True,
    }
    with pytest.raises(ManifestError, match="lacks requires-python"):
        release_manifest._discover_distributions(
            root,
            root,
            release_manifest._tracked_paths(root),
            commit,
        )


def test_dependency_input_report_rejects_source_change_after_generation(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    report = build_dependency_input_report(root, payload["candidate"]["source"]["commit"])
    _commit_fixture_file(root, "modules/00/pyproject.toml", "[project]\nname='changed'\n")

    with pytest.raises(ManifestError, match="source commit differs"):
        validate_dependency_input_report(report, root)


def test_runtime_lock_rejects_missing_source_dependency_even_with_matching_receipt(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace("dependencies = []", 'dependencies = ["PyYAML>=6.0"]')
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    # The existing lock and its regenerated reconstruction receipt agree with
    # each other, but omit the exact source-declared dependency.
    with pytest.raises(ManifestError, match="omits source-declared dependency pin: pyyaml"):
        validate_candidate(payload, root, bundle)


def test_runtime_lock_checks_all_overlapping_source_constraints(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    paths = ("modules/00/pyproject.toml", "modules/01/pyproject.toml")
    contents = []
    for path, requirement in zip(paths, ("shared-lib>=1,<4", "shared-lib>=2,<3"), strict=True):
        content = (root / path).read_text(encoding="utf-8")
        if "dependencies = []" in content:
            content = content.replace(
                "dependencies = []", f"dependencies = [{json.dumps(requirement)}]"
            )
        else:
            content = content.replace(
                "[build-system]", f"dependencies = [{json.dumps(requirement)}]\n[build-system]"
            )
        contents.append((path, content))
    _commit_descriptor_changes(root, bundle, payload, dict(contents))
    _replace_purpose_locks(
        bundle,
        payload,
        "runtime",
        _requirements_lock(("fixture-dependency", "1.0.0"), ("shared-lib", "2.5.0")),
    )
    validate_candidate(payload, root, bundle)


def test_runtime_lock_rejects_pin_outside_source_constraint(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace("dependencies = []", 'dependencies = ["sample-lib>=2,<3"]')
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    _replace_purpose_locks(
        bundle,
        payload,
        "runtime",
        _requirements_lock(("fixture-dependency", "1.0.0"), ("sample-lib", "1.9.0")),
    )
    with pytest.raises(ManifestError, match="violates source-declared constraints"):
        validate_candidate(payload, root, bundle)


def test_runtime_internal_dependency_uses_candidate_version_and_runtime_coverage(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace("dependencies = []", 'dependencies = ["fixture-distribution-01>=1.0.1,<2"]')
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    validate_candidate(payload, root, bundle)


def test_runtime_internal_dependency_preserves_declared_prerelease_semantics() -> None:
    # This is the exact source requirement/version pair in the clean product
    # source at 127c483659fbb3acdfc90ba39ecb3af2b4c69616.
    requirement = Requirement("modular-results-registry>=0.1.0.dev0,<0.2")
    source_policy = {
        "modular-strategy-factory": {"modular-results-registry": [requirement.specifier]}
    }
    candidate_component = {
        "id": "modular-results-registry",
        "version": "0.1.0.dev0",
    }
    release_manifest._validate_source_requirement_coverage(
        pins=(),
        covered_components=["modular-strategy-factory"],
        requirements_by_component=source_policy,
        components_by_normalized_name={"modular-results-registry": candidate_component},
        label="fixture runtime lock",
        purpose="runtime",
        runtime_lock_coverage={
            (
                "modular-results-registry",
                "python",
                "runtime",
                "owner-defined",
                "cpython-3.12.8",
            )
        },
        environment="owner-defined",
        runtime_id="cpython-3.12.8",
    )


def test_any_internal_package_pin_in_a_lock_must_match_candidate_version(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    lock = next(
        item for item in payload["candidate"]["locks"] if item["id"] == "python-runtime-312"
    )
    lock["covered_components"].remove("fixture-distribution-00")
    _replace_lock(
        bundle,
        payload,
        lock["id"],
        _requirements_lock(("fixture-dependency", "1.0.0"), ("fixture-distribution-00", "9.9.9")),
    )
    with pytest.raises(ManifestError, match="internal candidate pin differs"):
        validate_candidate(payload, root, bundle)


def test_static_optional_dependencies_remain_unselected_from_runtime_lock(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace(
            "[build-system]",
            '[project.optional-dependencies]\nresearch = ["optional-lib>=9"]\n\n[build-system]',
        )
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    validate_candidate(payload, root, bundle)


def test_v2_malformed_unselected_optional_dependencies_do_not_change_acceptance(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace(
            "[build-system]", 'optional-dependencies = "malformed-unselected-value"\n[build-system]'
        )
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    assert payload["schema_version"] == 2
    validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("project_fields", "message"),
    [
        ('dynamic = ["dependencies"]\n', "dynamic project.dependencies"),
        (
            'dependencies = ["sample-lib>=1"]\ndynamic = ["dependencies"]\n',
            "cannot be both static and dynamic",
        ),
        (
            "dependencies = [\"sample-lib>=1; sys_platform == 'linux'\"]\n",
            "unmodeled environment marker variables",
        ),
        ('dependencies = ["sample-lib @ https://example.invalid/sample.whl"]\n', "direct URL"),
        ('dependencies = ["sample-lib[fast]>=1"]\n', "dependency-requested extras"),
    ],
)
def test_source_runtime_requirements_reject_unsupported_forms(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    project_fields: str,
    message: str,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (root / descriptor).read_text(encoding="utf-8").replace("dependencies = []\n", "")
    content = content.replace("[build-system]", project_fields + "[build-system]")
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("requirements", "pins", "message"),
    [
        (
            ["setuptools>=68"],
            [("fixture-dependency", "1.0.0")],
            "omits source-declared dependency pin: setuptools",
        ),
        (
            ["wheel>=0.42"],
            [("fixture-dependency", "1.0.0")],
            "omits source-declared dependency pin: wheel",
        ),
        (
            ["setuptools>=68"],
            [("fixture-dependency", "1.0.0"), ("setuptools", "67.9.0")],
            "violates source-declared constraints",
        ),
        (
            ["hatchling<2"],
            [("fixture-dependency", "1.0.0"), ("hatchling", "2.0.0")],
            "violates source-declared constraints",
        ),
    ],
)
def test_build_lock_covers_static_backend_requirements(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    requirements: list[str],
    pins: list[tuple[str, str]],
    message: str,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace("requires = []", f"requires = {json.dumps(requirements)}")
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    _replace_purpose_locks(bundle, payload, "build", _requirements_lock(*pins))
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_build_lock_rejects_internal_backend_without_candidate_artifact_binding(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace("requires = []", 'requires = ["fixture-distribution-01>=1.0.1"]')
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    with pytest.raises(ManifestError, match="before candidate artifact binding"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("requirements", "pins", "message"),
    [
        (["backend-extra>=2"], {}, "omits or violates hook requirement"),
        (["backend-extra>=3"], {"backend-extra": "2.5"}, "omits or violates hook requirement"),
        (["backend-extra==2.5"], {"backend-extra": "2.5"}, "valid"),
        ([], {}, "valid"),
    ],
)
def test_hook_requirements_must_match_final_lock_pins(
    requirements: list[str], pins: dict[str, str], message: str
) -> None:
    if message == "valid":
        release_manifest._validate_hook_requirement_pins(requirements, pins)
    else:
        with pytest.raises(ManifestError, match=message):
            release_manifest._validate_hook_requirement_pins(requirements, pins)


def test_hook_attestation_schema_requires_explicit_empty_or_nonempty_result() -> None:
    schema = json.loads(Path(release_manifest.SCHEMA_PATH).read_text(encoding="utf-8"))
    hook = schema["$defs"]["hookAttestation"]
    successful_status = next(
        item
        for item in hook["allOf"]
        if item["if"]["properties"]["status"].get("enum") == ["returned", "optional_hook_absent"]
    )
    assert "returned_requirements" in successful_status["then"]["required"]
    assert hook["properties"]["returned_requirements"]["type"] == "array"
    assert hook["properties"]["status"]["enum"] == [
        "returned",
        "optional_hook_absent",
        "failed",
        "unavailable",
    ]
    assert hook["additionalProperties"] is False


def test_accepted_candidate_requires_typed_build_attestations(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    _accepted(payload, bundle, with_build_evidence=False)
    with pytest.raises(ManifestError, match="lacks passing typed wheel build evidence"):
        validate_candidate(payload, root, bundle)


def test_pending_candidate_accepts_complete_typed_build_chain_and_rejects_swaps(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    _context, _hook, build = _add_typed_build_chain(bundle, payload)
    validate_candidate(payload, root, bundle)

    original_ref = dict(build["artifact"])
    build["artifact"]["sha256"] = "f" * 64
    build["attestation_sha256"] = object_identity(build, "attestation_sha256")
    with pytest.raises(ManifestError, match="artifact SHA-256"):
        validate_candidate(payload, root, bundle)

    build["artifact"] = original_ref
    build["context_sha256"] = "e" * 64
    build["attestation_sha256"] = object_identity(build, "attestation_sha256")
    with pytest.raises(ManifestError, match="unknown context"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [("passed", True), ("failed", False), ("unavailable", False)],
)
def test_v3_passed_build_attestation_requires_isolated_context(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_error: bool,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = _make_v3_pending_case(repo_case)
    context, _hook, build = _add_typed_build_chain(bundle, payload)
    context["environment"]["isolation"] = "non_isolated"
    build["outcome"] = outcome
    if outcome != "passed":
        build.pop("artifact")
    _refresh_typed_build_links(payload)

    if expected_error:
        with pytest.raises(ManifestError, match="passing build attestation requires isolated"):
            validate_candidate(payload, root, bundle)
    else:
        assert validate_candidate(payload, root, bundle) is None


def test_acceptance_only_mac_environment_adds_no_lock_obligation(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    row = next(
        item
        for item in payload["support_profile"]["acceptance_requirements"]
        if item["gate"] == "operations:mac-service-reconnect"
    )
    row["environment"] = "mac-acceptance"
    mac_evidence = next(
        item
        for item in payload["evidence"]
        if item["gate"] == row["gate"] and item["component_id"] == row["component_id"]
    )
    mac_evidence["environment"] = "mac-acceptance"
    assert "mac-acceptance" not in payload["support_profile"]["target_environments"]
    assert all(
        item["target_environment"] != "mac-acceptance"
        for item in payload["support_profile"]["lock_requirements"]
    )
    _refresh(payload, bundle)
    validate_candidate(payload, root, bundle)
    mac_evidence["environment"] = "owner-defined"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="does not match a declared acceptance requirement"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("dimension", ["environment", "runtime_id"])
def test_compatibility_evidence_must_match_environment_and_runtime_scope(
    repo_case: tuple[Path, Path, dict[str, Any]],
    dimension: str,
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    _attach_compatibility_subject_evidence(payload, row)
    evidence = next(item for item in payload["evidence"] if "compatibility_subject_sha256" in item)
    evidence[dimension] = "unlisted-scope-value"
    _refresh(payload, bundle)
    with pytest.raises(
        ManifestError,
        match="undeclared acceptance environment|evidence scope, identity, or gate does not match",
    ):
        validate_candidate(payload, root, bundle)


def test_compatibility_subject_rejects_duplicate_evidence_for_one_scope(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    _attach_compatibility_subject_evidence(payload, row)
    original = next(item for item in payload["evidence"] if "compatibility_subject_sha256" in item)
    duplicate = dict(original)
    duplicate["run_id"] = "fixture-duplicate-subject-proof"
    payload["evidence"].append(duplicate)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="confirmed compatibility subject requires exactly one"):
        validate_candidate(payload, root, bundle)


def test_compatibility_subject_requires_evidence_for_every_scope(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    _attach_compatibility_subject_evidence(payload, row)
    evidence = next(item for item in payload["evidence"] if "compatibility_subject_sha256" in item)
    payload["evidence"].remove(evidence)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="confirmed compatibility subject requires exactly one"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate acceptance requirement"),
        ("unknown_gate", "unknown gate"),
        ("unknown_component", "unknown component"),
        ("missing_pair", "omit provider/gate pairs"),
    ],
)
def test_acceptance_mapping_is_explicit_and_complete(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    rows = payload["support_profile"]["acceptance_requirements"]
    if mutation == "duplicate":
        rows.append(dict(rows[0]))
    elif mutation == "unknown_gate":
        rows[0]["gate"] = "unknown-gate"
    elif mutation == "unknown_component":
        rows[0]["component_id"] = "unknown-component"
    else:
        rows[:] = [row for row in rows if row["gate"] != "operations:mac-service-reconnect"]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_evidence_cannot_be_swapped_to_another_acceptance_environment(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    payload["release_decision"]["decision"] = "pending"
    payload["release_decision"]["owner_approval"]["decision"] = "pending"
    evidence = next(item for item in payload["evidence"] if item["gate"] == "gate_00")
    evidence["environment"] = "mac-acceptance"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="undeclared acceptance environment"):
        validate_candidate(payload, root, bundle)


def test_acceptance_only_environment_cannot_receive_lock_reconstruction(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    payload["support_profile"]["acceptance_requirements"][0]["environment"] = "mac-acceptance"
    payload["lock_reconstructions"][0]["environment"] = "mac-acceptance"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="does not match a declared lock/runtime"):
        validate_candidate(payload, root, bundle)


def test_failed_hook_can_be_recorded_without_final_lock_or_requirement_output() -> None:
    schema = json.loads(Path(release_manifest.SCHEMA_PATH).read_text(encoding="utf-8"))
    hook = schema["$defs"]["hookAttestation"]
    assert not {
        "returned_requirements",
        "final_lock_id",
        "final_lock_sha256",
        "reconstruction_receipt_sha256",
    } & set(hook["required"])
    failed_condition = next(
        item
        for item in hook["allOf"]
        if item["if"]["properties"]["status"].get("enum") == ["failed", "unavailable"]
    )
    assert "not" in failed_condition["then"]


def test_failed_build_can_be_recorded_without_an_output_artifact() -> None:
    schema = json.loads(Path(release_manifest.SCHEMA_PATH).read_text(encoding="utf-8"))
    build = schema["$defs"]["buildAttestation"]
    assert "artifact" not in build["required"]
    assert any(
        item["if"]["properties"]["outcome"].get("enum") == ["failed", "unavailable"]
        and "not" in item["then"]
        for item in build["allOf"]
    )


def test_final_build_lock_preserves_bootstrap_artifact_hashes() -> None:
    sha_a, sha_b = "a" * 64, "b" * 64
    bootstrap = {"backend": ("1.2.0", {sha_a})}
    release_manifest._validate_preserved_bootstrap_hashes(
        bootstrap, {"backend": ("1.2.0", {sha_a})}
    )
    with pytest.raises(ManifestError, match="bootstrap package hashes"):
        release_manifest._validate_preserved_bootstrap_hashes(
            bootstrap, {"backend": ("1.2.0", {sha_b})}
        )
    with pytest.raises(ManifestError, match="bootstrap package hashes"):
        release_manifest._validate_preserved_bootstrap_hashes(
            bootstrap, {"backend": ("1.2.0", {sha_a, sha_b})}
        )


def test_accepted_build_coverage_is_runtime_specific_and_allows_identical_outputs() -> None:
    component = {"id": "sample", "kind": "python_distribution"}
    artifact = {"component": "sample", "kind": "distribution", "file": {"sha256": "d" * 64}}
    builds = {
        ("sample", runtime, "linux-x86_64", operation): {
            "outcome": "passed",
            "_artifact_sha256": "d" * 64,
        }
        for runtime in ("cpython-3.12.8", "cpython-3.13.1")
        for operation in ("wheel", "sdist")
    }
    release_manifest._validate_accepted_build_coverage(
        [component], set({"cpython-3.12.8", "cpython-3.13.1"}), {"linux-x86_64"}, builds, [artifact]
    )
    builds.pop(("sample", "cpython-3.13.1", "linux-x86_64", "sdist"))
    with pytest.raises(ManifestError, match="cpython-3.13.1"):
        release_manifest._validate_accepted_build_coverage(
            [component],
            set({"cpython-3.12.8", "cpython-3.13.1"}),
            {"linux-x86_64"},
            builds,
            [artifact],
        )


@pytest.mark.parametrize(
    "field",
    [
        "context_sha256",
        "operation",
        "final_lock_id",
        "final_lock_sha256",
        "reconstruction_receipt_sha256",
        "python_runtime_id",
    ],
)
def test_build_attestation_rejects_swapped_context_operation_or_lock_links(field: str) -> None:
    context = {
        "context_sha256": "c" * 64,
        "component_id": "sample",
        "python_runtime_id": "cpython-3.12.8",
        "target": {"environment": "linux-x86_64"},
    }
    hook = {
        "context_sha256": context["context_sha256"],
        "operation": "wheel",
        "status": "returned",
        "final_lock_id": "python-build-312",
        "final_lock_sha256": "l" * 64,
        "reconstruction_receipt_sha256": "r" * 64,
    }
    lock = {
        "purpose": "build",
        "id": "python-build-312",
        "file": {"sha256": "l" * 64},
        "python_runtime_id": "cpython-3.12.8",
        "target_environment": "linux-x86_64",
        "covered_components": ["sample"],
    }
    row = {
        "context_sha256": context["context_sha256"],
        "operation": "wheel",
        "final_lock_id": "python-build-312",
        "final_lock_sha256": "l" * 64,
        "reconstruction_receipt_sha256": "r" * 64,
        "python_runtime_id": "cpython-3.12.8",
        "target_environment": "linux-x86_64",
    }
    row[field] = "sdist" if field == "operation" else "x" * 64
    with pytest.raises(ManifestError):
        release_manifest._validate_build_attestation_links(row, context, hook, lock)


def test_build_lock_accepts_overlapping_compatible_constraints(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    replacements = {}
    for path, requirement in (
        ("modules/00/pyproject.toml", "shared-backend>=68,<71"),
        ("modules/01/pyproject.toml", "shared-backend>=69,<70"),
    ):
        replacements[path] = (
            (root / path)
            .read_text(encoding="utf-8")
            .replace("requires = []", f"requires = [{json.dumps(requirement)}]")
        )
    _commit_descriptor_changes(root, bundle, payload, replacements)
    _replace_purpose_locks(
        bundle,
        payload,
        "build",
        _requirements_lock(("fixture-dependency", "1.0.0"), ("shared-backend", "69.5.0")),
    )
    validate_candidate(payload, root, bundle)


def test_build_lock_rejects_missing_or_malformed_build_metadata(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _skip_optional_jsonschema_dependency(monkeypatch)
    root, bundle, payload = repo_case
    descriptor = "modules/00/pyproject.toml"
    content = (
        (root / descriptor)
        .read_text(encoding="utf-8")
        .replace('[build-system]\nrequires = []\nbuild-backend = "fixture.backend"\n', "")
    )
    _commit_descriptor_changes(root, bundle, payload, {descriptor: content})
    with pytest.raises(ManifestError, match="build-system metadata is missing or invalid"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("candidate"),
        lambda payload: payload.__setitem__("candidate", None),
        lambda payload: payload["candidate"].pop("source"),
        lambda payload: payload["candidate"].__setitem__("source", "malformed"),
        lambda payload: payload.pop("support_profile"),
        lambda payload: payload.__setitem__("support_profile", []),
    ],
    ids=(
        "candidate-absent",
        "candidate-malformed",
        "source-absent",
        "source-malformed",
        "support-profile-absent",
        "support-profile-malformed",
    ),
)
def test_cli_rejects_malformed_structure_without_traceback(
    repo_case: tuple[Path, Path, dict[str, Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutation: Any,
) -> None:
    root, bundle, payload = repo_case
    mutation(payload)
    manifest = tmp_path / "malformed-manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = release_manifest.main(
        [
            str(manifest),
            "--source-root",
            str(root),
            "--candidate-bundle-root",
            str(bundle),
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "manifest validation failed:" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("identity_field", "expected_error"),
    [
        ("filename", "wheel identity differs"),
        ("dist_info", ".dist-info identity differs"),
        ("metadata", "wheel identity differs"),
    ],
)
def test_python_wheel_filename_dist_info_and_metadata_match_component(
    repo_case: tuple[Path, Path, dict[str, Any]],
    identity_field: str,
    expected_error: str,
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    overrides: dict[str, Any] = {}
    if identity_field == "filename":
        overrides["filename_name"] = "other_distribution"
    elif identity_field == "dist_info":
        overrides["dist_info_name"] = "other_distribution"
    else:
        overrides["metadata_name"] = "other-distribution"
    filename, content = _build_wheel(component["id"], component["version"], **overrides)
    _replace_python_wheel(bundle, payload, component["id"], filename, content)
    with pytest.raises(ManifestError, match=expected_error):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("filename_tag", "wheel_tags"),
    [
        ("py3-none-any", ["py3-none-any"]),
        ("py2.py3-none-any", ["py2-none-any", "py3-none-any"]),
    ],
)
def test_portable_v3_wheel_policy_accepts_only_expanded_portable_tags(
    filename_tag: str, wheel_tags: list[str]
) -> None:
    name, version = "fixture-distribution-00", "1.0.0"
    content = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\n" + b"".join(
        f"Tag: {tag}\n".encode() for tag in wheel_tags
    )
    filename, wheel = _build_wheel(
        name,
        version,
        filename_tag=filename_tag,
        wheel_metadata=content,
        metadata_requires_python=[">=3.12"],
    )
    assert (
        release_manifest._validate_python_distribution_artifact(
            filename,
            wheel,
            {"id": name, "version": version},
            validate_portable_policy=True,
            runtime_versions=("3.12.8", "3.13.1"),
            source_python_specifier=release_manifest.SpecifierSet(">=3.12"),
        )
        == "wheel"
    )


def test_portable_v3_wheel_policy_accepts_equivalent_requires_python_order() -> None:
    name, version = "fixture-distribution-00", "1.0.0"
    filename, wheel = _build_wheel(
        name,
        version,
        metadata_requires_python=["<4, >=3.12"],
    )
    assert (
        release_manifest._validate_python_distribution_artifact(
            filename,
            wheel,
            {"id": name, "version": version},
            validate_portable_policy=True,
            runtime_versions=("3.12.8", "3.13.1"),
            source_python_specifier=release_manifest.SpecifierSet(">=3.12,<4"),
        )
        == "wheel"
    )


@pytest.mark.parametrize(
    ("filename_tag", "wheel_metadata", "error"),
    [
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py2-none-any\nTag: py3-none-any\n\n",
            "WHEEL tags differ from filename tags",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n\n",
            "unsupported WHEEL tags",
        ),
        (
            "cp312-cp312-manylinux_2_17_x86_64",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: cp312-cp312-manylinux_2_17_x86_64\n\n",
            "unsupported wheel filename tags",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 2.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "unsupported or duplicate Wheel-Version",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nWheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "unsupported or duplicate Wheel-Version",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n\n",
            "Root-Is-Purelib: true",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\nTag: cp312-cp312-manylinux_2_17_x86_64\n\n",
            "unsupported WHEEL tags",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "duplicate Requires-Python",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "invalid Requires-Python",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "incompatible with supported runtimes",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "differs from source package metadata",
        ),
        (
            "py3-none-any",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            "exactly one nonempty Requires-Python",
        ),
    ],
)
def test_portable_v3_wheel_policy_rejects_invalid_metadata(
    filename_tag: str,
    wheel_metadata: bytes,
    error: str,
) -> None:
    name, version = "fixture-distribution-00", "1.0.0"
    filename, wheel = _build_wheel(
        name,
        version,
        filename_tag=filename_tag,
        wheel_metadata=wheel_metadata,
        metadata_requires_python={
            "duplicate Requires-Python": [">=3.12", ">=3.12"],
            "invalid Requires-Python": [">=3.x"],
            "incompatible with supported runtimes": ["<3.13"],
            "differs from source package metadata": [">=3.12"],
        }.get(error),
    )
    with pytest.raises(ManifestError, match=error):
        release_manifest._validate_python_distribution_artifact(
            filename,
            wheel,
            {"id": name, "version": version},
            validate_portable_policy=True,
            runtime_versions=("3.12.8", "3.13.1"),
            source_python_specifier=(
                release_manifest.SpecifierSet(">=3.12,<4")
                if error == "differs from source package metadata"
                else release_manifest.SpecifierSet("<3.13")
                if error == "incompatible with supported runtimes"
                else release_manifest.SpecifierSet(">=3.12")
            ),
        )


@pytest.mark.parametrize("wheel_members", [[], ["second"]])
def test_portable_v3_wheel_policy_requires_one_corresponding_wheel_member(
    wheel_members: list[str],
) -> None:
    name, version = "fixture-distribution-00", "1.0.0"
    filename, content = _build_wheel(name, version)
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as original,
        zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        for member in original.infolist():
            if member.filename.endswith(".dist-info/WHEEL") and not wheel_members:
                continue
            archive.writestr(member, original.read(member.filename))
        if wheel_members:
            archive.writestr(
                "second-1.0.dist-info/WHEEL",
                b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
            )
    with pytest.raises(ManifestError, match="exactly one corresponding .dist-info/WHEEL"):
        release_manifest._validate_python_distribution_artifact(
            filename,
            buffer.getvalue(),
            {"id": name, "version": version},
            validate_portable_policy=True,
            runtime_versions=("3.12.8", "3.13.1"),
        )


def test_component_inventory_still_rejects_unrecognized_extra_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console_name = "@module-strategy/market-data-console"
    console_bytes = b'{"name":"@module-strategy/market-data-console","version":"1.0.0"}'
    monkeypatch.setattr(release_manifest, "_discover_distributions", lambda *args: {})
    monkeypatch.setattr(release_manifest, "_read_path", lambda *args: console_bytes)
    components = [
        {
            "id": console_name,
            "kind": "web_console",
            "version": "1.0.0",
            "descriptor": {
                "origin": "source",
                "path": "web/market-data-console/package.json",
                "sha256": sha256_bytes(console_bytes),
            },
            "source_files": [],
        },
        {"id": "unexpected-distribution", "kind": "python_distribution"},
    ]
    with pytest.raises(ManifestError, match="component inventory has unknown package"):
        release_manifest._check_component_inventory(
            tmp_path, tmp_path, components, set(), "a" * 40, []
        )


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        (
            "fixture_distribution_00-1.0.0-py3-none-any.whl",
            b"not a zip archive",
            "lacks a valid ZIP end-of-central-directory record",
        ),
        (
            "fixture_distribution_00-1.0.0-py3-none-any.whl",
            _build_wheel(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[
                    (
                        "second-1.0.dist-info/METADATA",
                        _core_metadata("fixture-distribution-00", "1.0.0"),
                        None,
                    )
                ],
            )[1],
            "exactly one .dist-info/METADATA",
        ),
        (
            "fixture_distribution_00-1.0.0-py3-none-any.whl",
            _build_wheel(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[("../escape", b"x", None)],
            )[1],
            "unsafe archive path",
        ),
        (
            "fixture_distribution_00-1.0.0-py3-none-any.whl",
            _build_wheel(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[("link", b"target", stat.S_IFLNK)],
            )[1],
            "unsupported archive member kind",
        ),
    ],
)
def test_python_wheel_malformed_duplicate_traversal_and_unsupported_members_fail_closed(
    repo_case: tuple[Path, Path, dict[str, Any]],
    filename: str,
    content: bytes,
    expected_error: str,
) -> None:
    root, bundle, payload = repo_case
    component_id = payload["candidate"]["components"][0]["id"]
    _replace_python_wheel(bundle, payload, component_id, filename, content)
    with pytest.raises(ManifestError, match=expected_error):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("bound", ["compressed", "expanded", "entries", "metadata"])
def test_python_wheel_archive_resource_bounds_are_enforced(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    if bound == "compressed":
        monkeypatch.setattr(release_manifest, "MAX_DISTRIBUTION_ARCHIVE_BYTES", 1)
        message = "compressed-size limit"
    elif bound == "expanded":
        monkeypatch.setattr(release_manifest, "MAX_DISTRIBUTION_EXPANDED_BYTES", 8)
        message = "expanded-size limit"
    elif bound == "entries":
        monkeypatch.setattr(release_manifest, "MAX_DISTRIBUTION_ARCHIVE_ENTRIES", 1)
        message = "entry-count limit"
    else:
        monkeypatch.setattr(release_manifest, "MAX_DISTRIBUTION_METADATA_BYTES", 8)
        message = "metadata size limit"
    if bound == "entries":
        filename, content = _build_wheel(
            component["id"],
            component["version"],
            extra_members=[("payload.bin", b"extra", None)],
        )
        _replace_python_wheel(bundle, payload, component["id"], filename, content)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_python_wheel_entry_limit_precedes_zipfile_materialization(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    filename, content = _build_wheel(
        component["id"], component["version"], extra_members=[("payload.bin", b"x", None)]
    )
    _replace_python_wheel(bundle, payload, component["id"], filename, content)
    monkeypatch.setattr(release_manifest, "MAX_DISTRIBUTION_ARCHIVE_ENTRIES", 1)

    def forbidden_zipfile(*args: Any, **kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before enforcing the central-directory entry bound")

    monkeypatch.setattr(release_manifest.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(ManifestError, match="archive entry-count limit"):
        validate_candidate(payload, root, bundle)


def test_python_wheel_rejects_zip64_before_zipfile_materialization(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    filename, content = _build_wheel(component["id"], component["version"])
    eocd_offset = content.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    zip64_content = bytearray(content)
    zip64_content[eocd_offset + 10 : eocd_offset + 12] = b"\xff\xff"
    _replace_python_wheel(bundle, payload, component["id"], filename, bytes(zip64_content))

    def forbidden_zipfile(*args: Any, **kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before rejecting unsupported ZIP64 layout")

    monkeypatch.setattr(release_manifest.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(ManifestError, match="unsupported ZIP64 layout"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("disk_start", 1, "unsupported multi-disk ZIP layout"),
        ("local_offset", 0xFFFFFFFF, "unsupported ZIP64 layout"),
    ],
)
def test_python_wheel_rejects_central_directory_disk_and_offset_before_zipfile(
    repo_case: tuple[Path, Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
    error: str,
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    filename, content = _build_wheel(component["id"], component["version"])
    eocd_offset = content.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    cd_offset = int.from_bytes(content[eocd_offset + 16 : eocd_offset + 20], "little")
    mutated = bytearray(content)
    if field == "disk_start":
        mutated[cd_offset + 34 : cd_offset + 36] = value.to_bytes(2, "little")
    else:
        mutated[cd_offset + 42 : cd_offset + 46] = value.to_bytes(4, "little")
    _replace_python_wheel(bundle, payload, component["id"], filename, bytes(mutated))

    def forbidden_zipfile(*args: Any, **kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before central-directory bounds were validated")

    monkeypatch.setattr(release_manifest.zipfile, "ZipFile", forbidden_zipfile)
    with pytest.raises(ManifestError, match=error):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"filename_name": "other-distribution"}, "sdist identity differs"),
        ({"root_name": "other-distribution"}, "sdist identity differs"),
        ({"metadata_name": "other-distribution"}, "sdist identity differs"),
        ({"pyproject_name": "other-distribution"}, "sdist identity differs"),
        ({"pyproject_version": "9.9.9"}, "sdist identity differs"),
    ],
)
def test_python_sdist_filename_root_and_metadata_match_component(
    repo_case: tuple[Path, Path, dict[str, Any]],
    overrides: dict[str, str],
    expected_error: str,
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    component = payload["candidate"]["components"][0]
    filename, content = _build_sdist(component["id"], component["version"], **overrides)
    _replace_python_sdist(bundle, payload, component["id"], filename, content)
    with pytest.raises(ManifestError, match=expected_error):
        validate_candidate(payload, root, bundle)


def test_python_sdist_dynamic_version_uses_generated_pkg_info(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    pyproject = f'[project]\nname = "{component["id"]}"\ndynamic = ["version"]\n'.encode()
    filename, content = _build_sdist(
        component["id"], component["version"], pyproject_content=pyproject
    )
    assert (
        release_manifest._validate_python_distribution_artifact(filename, content, component)
        == "sdist"
    )


@pytest.mark.parametrize(
    ("pyproject", "message"),
    [
        (
            b'[project]\nname = "fixture-distribution-00"\n'
            b'version = "1.0.0"\ndynamic = ["version"]\n',
            "declares version both statically and dynamically",
        ),
        (
            b'[project]\nname = "fixture-distribution-00"\ndynamic = ["version", "version"]\n',
            "malformed project.dynamic",
        ),
        (
            b'[project]\nname = "fixture-distribution-00"\ndynamic = "version"\n',
            "malformed project.dynamic",
        ),
    ],
)
def test_python_sdist_rejects_conflicting_or_malformed_dynamic_version(
    repo_case: tuple[Path, Path, dict[str, Any]], pyproject: bytes, message: str
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    component_id = component["id"]
    filename, content = _build_sdist(component_id, "1.0.0", pyproject_content=pyproject)
    with pytest.raises(ManifestError, match=message):
        release_manifest._validate_python_distribution_artifact(filename, content, component)


@pytest.mark.parametrize(
    ("artifact_format", "metadata_version", "message"),
    [
        ("wheel", "not-a-version", "invalid Metadata-Version"),
        ("wheel", "1.0", "below required 1.1"),
        ("wheel", "999.0", "unsupported Metadata-Version major"),
        ("sdist", "2.2.0", "invalid Metadata-Version"),
        ("sdist", "2.1", "below required 2.2"),
        ("sdist", "999.0", "unsupported Metadata-Version major"),
    ],
)
def test_python_distribution_requires_valid_minimum_metadata_version(
    repo_case: tuple[Path, Path, dict[str, Any]],
    artifact_format: str,
    metadata_version: str,
    message: str,
) -> None:
    _, _, payload = repo_case
    component = payload["candidate"]["components"][0]
    if artifact_format == "wheel":
        filename, content = _build_wheel(
            component["id"],
            component["version"],
            metadata_format_version=metadata_version,
        )
    else:
        filename, content = _build_sdist(
            component["id"],
            component["version"],
            metadata_format_version=metadata_version,
        )
    with pytest.raises(ManifestError, match=message):
        release_manifest._validate_python_distribution_artifact(filename, content, component)


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        (
            "fixture-distribution-00-1.0.0.tar.gz",
            b"not a gzip tar",
            "not a valid bounded modern sdist archive",
        ),
        (
            "fixture-distribution-00-1.0.0.tar.gz",
            _build_sdist(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[
                    (
                        "fixture-distribution-00-1.0.0/PKG-INFO",
                        _core_metadata("fixture-distribution-00", "1.0.0"),
                        None,
                    )
                ],
            )[1],
            "duplicate archive paths",
        ),
        (
            "fixture-distribution-00-1.0.0.tar.gz",
            _build_sdist(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[("fixture-distribution-00-1.0.0/../escape", b"x", None)],
            )[1],
            "unsafe archive path",
        ),
        (
            "fixture-distribution-00-1.0.0.tar.gz",
            _build_sdist(
                "fixture-distribution-00",
                "1.0.0",
                extra_members=[("fixture-distribution-00-1.0.0/link", b"target", tarfile.SYMTYPE)],
            )[1],
            "unsupported archive member kind",
        ),
    ],
)
def test_python_sdist_malformed_duplicate_traversal_and_unsupported_members_fail_closed(
    repo_case: tuple[Path, Path, dict[str, Any]],
    filename: str,
    content: bytes,
    expected_error: str,
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    component_id = payload["candidate"]["components"][0]["id"]
    _replace_python_sdist(bundle, payload, component_id, filename, content)
    with pytest.raises(ManifestError, match=expected_error):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("missing_format", ["wheel", "sdist"])
def test_accepted_candidate_removing_distribution_invalidates_build_evidence(
    repo_case: tuple[Path, Path, dict[str, Any]], missing_format: str
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    component_id = payload["candidate"]["components"][0]["id"]
    suffix = ".whl" if missing_format == "wheel" else ".tar.gz"
    payload["candidate"]["artifacts"] = [
        item
        for item in payload["candidate"]["artifacts"]
        if not (item["component"] == component_id and item["file"]["path"].endswith(suffix))
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="build attestation artifact is not bound"):
        validate_candidate(payload, root, bundle)


def test_pending_candidate_may_have_only_a_wheel(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    validate_candidate(payload, root, bundle)


def test_python_component_rejects_non_distribution_artifact_kind(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["artifacts"][0]["kind"] = "other"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="artifacts must use distribution kind"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_313", "is too short"),
        ("opaque_id", "does not match"),
        ("version_id_mismatch", "exact stable CPython identity"),
    ],
)
def test_python_runtime_identity_is_exact_and_covers_required_minors(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str, message: str
) -> None:
    root, bundle, payload = repo_case
    runtimes = payload["support_profile"]["python_runtimes"]
    if mutation == "missing_313":
        runtimes.pop()
        _refresh(payload, bundle, refresh_receipts=False)
    elif mutation == "opaque_id":
        runtimes[0]["id"] = "runtime-a"
        _refresh(payload, bundle, refresh_receipts=False)
    else:
        runtimes[0]["version"] = "3.12.9"
        _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_source_requires_python_must_include_each_exact_runtime(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, python_requires=">=3.13")
    with pytest.raises(ManifestError, match="does not satisfy .* requires-python >=3.13"):
        validate_candidate(payload, root, bundle)


def test_internal_dependency_range_accepts_selected_source_version(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, python_dependencies=["FIXTURE_Distribution_01>=1.0.1,<2.0"]
    )
    validate_candidate(payload, root, bundle)


def test_internal_dependency_range_rejects_incompatible_selected_version(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, python_dependencies=["fixture-distribution-01<1.0.1"]
    )
    with pytest.raises(
        ManifestError, match="selected Python component version does not satisfy dependency"
    ):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("dependency", "message"),
    [
        ("fixture-distribution-01>=1; python_version >= '3.12'", "marker is unsupported"),
        ("fixture-distribution-01>=1; python_version < '3.12'", "marker is unsupported"),
        ("fixture-distribution-01[fast]>=1", "requests extras"),
        (
            "fixture-distribution-01 @ https://example.invalid/package.whl",
            "uses a direct URL",
        ),
        ("fixture distribution !!!", "invalid Python dependency"),
    ],
)
def test_internal_dependency_unsupported_forms_fail_closed(
    tmp_path: Path, dependency: str, message: str
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, python_dependencies=[dependency])
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("marker", "applicable_lock"),
    [
        ("python_version >= '3.13'", "python-runtime-313"),
        ("python_version < '3.13'", "python-runtime-312"),
    ],
)
def test_external_dependency_marker_cannot_weaken_cross_minor_lock_parity(
    tmp_path: Path, marker: str, applicable_lock: str
) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, python_dependencies=[f"external-fixture-package>=1; {marker}"]
    )
    lock = next(item for item in payload["candidate"]["locks"] if item["id"] == applicable_lock)
    lock_path = bundle / lock["file"]["path"]
    content = (
        lock_path.read_bytes()
        + (f"external-fixture-package==1.2.3 \\\n  --hash=sha256:{'b' * 64}\n").encode()
    )
    _replace_lock(bundle, payload, applicable_lock, content)
    with pytest.raises(ManifestError, match="inventories differ across Python minors"):
        validate_candidate(payload, root, bundle)


def test_external_dependency_marker_requires_pin_when_true_for_runtime(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, python_dependencies=["external-fixture-package>=1; python_version >= '3.13'"]
    )
    with pytest.raises(
        ManifestError, match="omits source-declared dependency pin: external-fixture-package"
    ):
        validate_candidate(payload, root, bundle)


def test_external_dependency_platform_marker_fails_closed(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, python_dependencies=["external-fixture-package>=1; sys_platform == 'linux'"]
    )
    with pytest.raises(
        ManifestError, match="unmodeled environment marker variables: \\[.*sys_platform"
    ):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("field", ["lock_sha256", "python_runtime_id", "candidate_id"])
def test_lock_reconstruction_receipt_identity_is_bound_to_candidate(
    repo_case: tuple[Path, Path, dict[str, Any]], field: str
) -> None:
    root, bundle, payload = repo_case
    row = payload["lock_reconstructions"][0]
    _rewrite_receipt(bundle, payload, row, lambda receipt: receipt.__setitem__(field, "f" * 64))
    with pytest.raises(ManifestError, match="receipt identity mismatch"):
        validate_candidate(payload, root, bundle)


def test_lock_reconstruction_inventories_must_match_across_python_minors(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = payload["lock_reconstructions"][0]
    digest = "a" * 64
    _replace_lock(
        bundle,
        payload,
        row["lock_id"],
        f"other-dependency==2.0 --hash=sha256:{digest}\n".encode(),
    )
    _rewrite_receipt(
        bundle,
        payload,
        row,
        lambda receipt: receipt.__setitem__(
            "resolved_distributions", [{"name": "other-dependency", "version": "2.0"}]
        ),
    )
    with pytest.raises(ManifestError, match="inventories differ across Python minors"):
        validate_candidate(payload, root, bundle)


def test_lock_reconstruction_receipt_observed_runtime_is_exact(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = payload["lock_reconstructions"][0]
    _rewrite_receipt(
        bundle,
        payload,
        row,
        lambda receipt: receipt["observed_runtime"].__setitem__("version", "3.12.99"),
    )
    with pytest.raises(ManifestError, match="observed runtime mismatch"):
        validate_candidate(payload, root, bundle)


def test_lock_reconstruction_receipt_outcome_must_be_typed(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = payload["lock_reconstructions"][0]
    _rewrite_receipt(bundle, payload, row, lambda receipt: receipt.__setitem__("outcome", []))
    with pytest.raises(ManifestError, match="run identity is invalid"):
        validate_candidate(payload, root, bundle)


def test_every_python_lock_requires_a_reconstruction_receipt(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["lock_reconstructions"].pop()
    _refresh(payload, bundle)
    payload["lock_reconstructions"].pop()
    with pytest.raises(ManifestError, match="lack exact runtime reconstruction receipts"):
        validate_candidate(payload, root, bundle)


def test_lock_reconstruction_empty_inventory_is_representable(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    digest = "a" * 64
    candidate_lock = f"fixture-distribution-00==1.0.0 --hash=sha256:{digest}\n".encode()
    for lock in payload["candidate"]["locks"]:
        if lock["ecosystem"] == "python":
            _replace_lock(bundle, payload, lock["id"], candidate_lock)
    for row in payload["lock_reconstructions"]:
        _rewrite_receipt(
            bundle, payload, row, lambda receipt: receipt.__setitem__("resolved_distributions", [])
        )
    _refresh(payload, bundle, refresh_receipts=False)
    validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("inventory", "message"),
    [
        ([], "differs from exact lock pins"),
        (
            [
                {"name": "fixture-dependency", "version": "1.0.0"},
                {"name": "extra", "version": "2.0"},
            ],
            "differs from exact lock pins",
        ),
        ([{"name": "fixture-dependency", "version": "2.0"}], "differs from exact lock pins"),
    ],
)
def test_python_lock_reconstruction_inventory_must_match_lock_pins(
    repo_case: tuple[Path, Path, dict[str, Any]],
    inventory: list[dict[str, str]],
    message: str,
) -> None:
    root, bundle, payload = repo_case
    for row in payload["lock_reconstructions"]:
        if row["lock_id"] in {"python-build-312", "python-build-313"}:
            _rewrite_receipt(
                bundle,
                payload,
                row,
                lambda receipt: receipt.__setitem__("resolved_distributions", inventory),
            )
    _refresh(payload, bundle, refresh_receipts=False)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_python_lock_and_receipt_inventory_normalize_names_and_versions(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    digest = "a" * 64
    for lock in payload["candidate"]["locks"]:
        if lock["ecosystem"] == "python":
            _replace_lock(
                bundle,
                payload,
                lock["id"],
                f"FIXTURE_Dependency==1.0 --hash=sha256:{digest}\n".encode(),
            )
    for row in payload["lock_reconstructions"]:
        _rewrite_receipt(
            bundle,
            payload,
            row,
            lambda receipt: receipt.__setitem__(
                "resolved_distributions", [{"name": "fixture_dependency", "version": "1.0.0.0"}]
            ),
        )
    _refresh(payload, bundle, refresh_receipts=False)
    validate_candidate(payload, root, bundle)


def test_python_lock_candidate_pin_must_match_selected_component_version(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    digest = "a" * 64
    _replace_lock(
        bundle,
        payload,
        "python-runtime-312",
        f"fixture-distribution-00==9.9.9 --hash=sha256:{digest}\n".encode(),
    )
    with pytest.raises(ManifestError, match="candidate distribution pin differs"):
        validate_candidate(payload, root, bundle)


def test_python_lock_candidate_pin_is_excluded_from_receipt_inventory(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    digest = "a" * 64
    lock_content = f"fixture-distribution-00==1.0.0 --hash=sha256:{digest}\n".encode()
    for lock in payload["candidate"]["locks"]:
        if lock["ecosystem"] == "python":
            _replace_lock(bundle, payload, lock["id"], lock_content)
    for row in payload["lock_reconstructions"]:
        _rewrite_receipt(
            bundle, payload, row, lambda receipt: receipt.__setitem__("resolved_distributions", [])
        )
    _refresh(payload, bundle, refresh_receipts=False)
    validate_candidate(payload, root, bundle)


def test_generic_evidence_cannot_substitute_for_lock_reconstruction(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    evidence = dict(payload["evidence"][0])
    evidence["gate"] = "rebuild:python-runtime-312"
    evidence["evidence_sha256"] = object_identity(evidence, "evidence_sha256")
    payload["evidence"].append(evidence)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="cannot be satisfied by generic evidence"):
        validate_candidate(payload, root, bundle)


def test_production_component_inventory_covers_actual_distribution_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/platform-release-manifest-v2.schema.json").read_text())
    inventory = json.loads((root / "profiles/component-inventory.json").read_text())
    roles = inventory["roles"]
    capabilities = schema["$defs"]["profile"]["properties"]["capabilities"]["properties"]
    capability_names = [role["capability"] for role in roles]

    assert len(capability_names) == len(set(capability_names))
    assert set(capability_names) == set(capabilities)
    assert all(role["id"] == role["capability"] and role["required"] for role in roles)

    projects = [root / "pyproject.toml"]
    for product_root in ("applications", "modules", "plugins", "research"):
        projects.extend((root / product_root).rglob("pyproject.toml"))
    python_distributions: set[str] = set()
    for descriptor in projects:
        project = tomllib.loads(descriptor.read_text(encoding="utf-8")).get("project", {})
        if isinstance(project.get("name"), str):
            python_distributions.add(project["name"])
    console_metadata = json.loads(
        (root / "web/market-data-console/package.json").read_text(encoding="utf-8")
    )
    expected_providers = python_distributions | {console_metadata["name"]}
    actual_providers = {provider for role in roles for provider in role["providers"]}
    assert python_distributions == set(
        next(role["providers"] for role in roles if role["capability"] == "python_distributions")
    )
    assert {console_metadata["name"]} == set(
        next(role["providers"] for role in roles if role["capability"] == "market_data_web_console")
    )
    assert actual_providers == expected_providers

    gates = [gate for role in roles for gate in role["required_gates"]]
    assert len(gates) == len(set(gates))
    assert all(release_manifest.GATE_ID_RE.fullmatch(gate) for gate in gates)
    for role in roles:
        assert role["required_gates"]
        assert all(
            dependency["type"] == "github_issue"
            and release_manifest.DEPENDENCY_ISSUE_RE.fullmatch(dependency["url"])
            for dependency in role["blocked_by"]
        )

    blockers = {
        role["capability"]: [
            dependency["url"].rsplit("/", 1)[-1] for dependency in role["blocked_by"]
        ]
        for role in roles
    }
    assert blockers == {
        "python_distributions": ["982"],
        "market_data_web_console": ["370"],
        "operator_ui_control": ["370"],
        "recovery_ui": ["778"],
        "owner_native_composition": ["933"],
        "topology": ["400"],
        "real_host": ["873"],
        "lifecycle": ["395"],
        "drift": ["380"],
        "risk_supervisor": [],
        "portfolio": ["396"],
        "security": ["393"],
        "runbooks": ["400"],
        "recovery": [],
        "ab_soak": ["369"],
        "soak_2h": ["866"],
        "soak_24h": ["369"],
    }
    assert blockers["portfolio"] == ["396"]
    assert all(
        issue != "396"
        for capability, issues in blockers.items()
        if capability != "portfolio"
        for issue in issues
    )


def test_inventory_requires_each_capability_once_and_unique_gate_ids(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _bundle, payload = repo_case
    inventory = json.loads((root / "profiles/component-inventory.json").read_text())
    roles = inventory["roles"]
    capabilities = payload["support_profile"]["capabilities"]
    known_gates = {
        gate for capability in capabilities.values() for gate in capability["required_gates"]
    }
    components = payload["candidate"]["components"]

    with pytest.raises(ManifestError, match="cover each capability exactly once"):
        release_manifest._validate_inventory_relationships(
            roles[:-1], components, capabilities, known_gates
        )

    duplicate_gate_roles = [dict(role) for role in roles]
    duplicate_gate_roles[1] = dict(duplicate_gate_roles[1])
    duplicate_gate_roles[1]["required_gates"] = [roles[0]["required_gates"][0]]
    with pytest.raises(ManifestError, match="duplicate component inventory gate identity"):
        release_manifest._validate_inventory_relationships(
            duplicate_gate_roles, components, capabilities, known_gates
        )


@pytest.mark.parametrize("status", ["required", "supported"])
def test_inventory_blocker_requires_candidate_capability_to_remain_blocked(
    tmp_path: Path, status: str
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, {"recovery_ui"})
    payload["support_profile"]["capabilities"]["recovery_ui"]["status"] = status
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="must be blocked for unresolved required roles"):
        validate_candidate(payload, root, bundle)


def test_unblocked_required_capability_can_move_to_supported(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    capability = payload["support_profile"]["capabilities"]["risk_supervisor"]
    capability["status"] = "required"
    _refresh(payload, bundle)
    validate_candidate(payload, root, bundle)
    capability["status"] = "supported"
    _refresh(payload, bundle)
    validate_candidate(payload, root, bundle)


def test_required_providerless_role_needs_blocker_and_blocked_role_is_representable(
    tmp_path: Path,
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, {"portfolio"})
    inventory = json.loads((root / "profiles/component-inventory.json").read_text())
    roles = inventory["roles"]
    role = next(item for item in roles if item["capability"] == "portfolio")
    role["providers"] = []
    capabilities = payload["support_profile"]["capabilities"]
    capabilities["portfolio"]["components"] = []
    known_gates = {
        gate for capability in capabilities.values() for gate in capability["required_gates"]
    }
    release_manifest._validate_inventory_relationships(
        roles, payload["candidate"]["components"], capabilities, known_gates
    )

    role["blocked_by"] = []
    with pytest.raises(ManifestError, match="has no provider or blocked_by dependency"):
        release_manifest._validate_inventory_relationships(
            roles, payload["candidate"]["components"], capabilities, known_gates
        )


def test_python_lock_rejects_unhashed_requirements(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    _replace_lock(
        bundle,
        payload,
        "python-runtime-312",
        b"fixture-dependency==1.0.0\n",
        refresh_receipts=False,
    )
    with pytest.raises(ManifestError, match="every requirement must have SHA-256 hashes"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            b"fixture-dependency>=1.0.0 --hash=sha256:" + b"a" * 64 + b"\n",
            "exact non-wildcard == pin",
        ),
        (
            b"-r requirements.txt --hash=sha256:" + b"a" * 64 + b"\n",
            "unsupported requirement",
        ),
    ],
)
def test_python_lock_rejects_ranges_and_includes(
    repo_case: tuple[Path, Path, dict[str, Any]], content: bytes, message: str
) -> None:
    root, bundle, payload = repo_case
    _replace_lock(bundle, payload, "python-runtime-312", content, refresh_receipts=False)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    "requirement",
    [
        "fixture-dependency==1.0.0; python_version >= '3.12'",
        "fixture-dependency==1.0.0; os_name == 'posix'",
        "fixture-dependency @ https://example.invalid/fixture.whl",
    ],
)
def test_python_lock_rejects_markers_and_direct_urls(
    repo_case: tuple[Path, Path, dict[str, Any]], requirement: str
) -> None:
    root, bundle, payload = repo_case
    content = f"{requirement} --hash=sha256:{'a' * 64}\n".encode()
    _replace_lock(bundle, payload, "python-runtime-312", content, refresh_receipts=False)
    message = "does not allow requirement markers" if ";" in requirement else "direct URL"
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_python_lock_does_not_count_hash_option_inside_marker_string(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    fake_hash = "a" * 64
    content = (
        f'fixture-dependency==1.0.0; python_version == "3.12 --hash=sha256:{fake_hash}"\n'
    ).encode()
    _replace_lock(bundle, payload, "python-runtime-312", content, refresh_receipts=False)
    with pytest.raises(ManifestError, match="every requirement must have SHA-256 hashes"):
        validate_candidate(payload, root, bundle)


def test_python_lock_rejects_environment_expansion(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    content = (
        'fixture-dependency==1.0.0; python_version == "${PYTHON_VERSION}" '
        f"--hash=sha256:{'a' * 64}\n"
    ).encode()
    _replace_lock(bundle, payload, "python-runtime-312", content, refresh_receipts=False)
    with pytest.raises(ManifestError, match="environment variable expansion"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            f"--hash=sha256:{'a' * 64} fixture-dependency==1.0.0\n",
            "hash options must follow a requirement and marker",
        ),
        (
            f'fixture-dependency==1.0.0; --hash=sha256:{"a" * 64} os_name == "posix"\n',
            "malformed, unsupported, or misplaced hash option",
        ),
    ],
)
def test_python_lock_rejects_hash_options_outside_trailing_hash_list(
    repo_case: tuple[Path, Path, dict[str, Any]], content: str, message: str
) -> None:
    root, bundle, payload = repo_case
    _replace_lock(bundle, payload, "python-runtime-312", content.encode(), refresh_receipts=False)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("hash_count", [1, 2])
def test_python_lock_accepts_multiple_trailing_hash_options(
    repo_case: tuple[Path, Path, dict[str, Any]], hash_count: int
) -> None:
    root, bundle, payload = repo_case
    hashes = " ".join(f"--hash=sha256:{'a' * 63 + str(index)}" for index in range(hash_count))
    content = (f"fixture-dependency==1.0.0 {hashes}\n").encode()
    _replace_lock(bundle, payload, "python-runtime-312", content)
    validate_candidate(payload, root, bundle)


def test_python_lock_continuation_preserves_quoted_marker_text_then_rejects_marker(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    digest = "a" * 64
    content = (
        f'fixture-dependency==1.0.0; os_name == "po\\\nsix" --hash=sha256:{digest}\n'
    ).encode()
    assert release_manifest._requirements_logical_lines(content, "python-runtime-312") == [
        f'fixture-dependency==1.0.0; os_name == "posix" --hash=sha256:{digest}'
    ]
    _replace_lock(bundle, payload, "python-runtime-312", content, refresh_receipts=False)
    with pytest.raises(ManifestError, match="does not allow requirement markers"):
        validate_candidate(payload, root, bundle)


def test_python_lock_accepts_semantics_preserving_hash_list_continuation(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    content = (
        f"fixture-dependency==1.0.0 \\\n  --hash=sha256:{'a' * 64} \\\n  --hash=sha256:{'b' * 64}\n"
    ).encode()
    _replace_lock(bundle, payload, "python-runtime-312", content)
    validate_candidate(payload, root, bundle)


def test_lock_resolver_version_must_be_concrete(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    lock = next(
        item for item in payload["candidate"]["locks"] if item["id"] == "python-runtime-312"
    )
    lock["version"] = "owner-defined"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="resolver version must be one concrete version"):
        validate_candidate(payload, root, bundle)


def test_npm_lock_root_identity_must_match_source_descriptor(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    lock_path = bundle / "locks/npm-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["version"] = "9.9.9"
    content = json.dumps(lock, sort_keys=True).encode("utf-8")
    _replace_lock(bundle, payload, "console-runtime", content)
    with pytest.raises(ManifestError, match="top-level package identity differs from package.json"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("lockfile_version", [True, 2])
def test_npm_lock_requires_integer_format_v3(
    repo_case: tuple[Path, Path, dict[str, Any]], lockfile_version: int | bool
) -> None:
    root, bundle, payload = repo_case
    lock_path = bundle / "locks/npm-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["lockfileVersion"] = lockfile_version
    content = json.dumps(lock, sort_keys=True).encode("utf-8")
    _replace_lock(bundle, payload, "console-runtime", content)
    with pytest.raises(ManifestError, match="format version 3"):
        validate_candidate(payload, root, bundle)


def test_npm_lock_requires_valid_package_integrity(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    lock_path = bundle / "locks/npm-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    package_path = next(path for path in lock["packages"] if path)
    lock["packages"][package_path]["integrity"] = "sha512-***"
    content = json.dumps(lock, sort_keys=True).encode("utf-8")
    _replace_lock(bundle, payload, "console-runtime", content)
    with pytest.raises(ManifestError, match="malformed SRI integrity"):
        validate_candidate(payload, root, bundle)


def test_npm_lock_rejects_whitespace_only_package_integrity(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    lock_path = bundle / "locks/npm-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    package_path = next(path for path in lock["packages"] if path)
    lock["packages"][package_path]["integrity"] = " \t\n"
    content = json.dumps(lock, sort_keys=True).encode("utf-8")
    _replace_lock(bundle, payload, "console-runtime", content)
    with pytest.raises(ManifestError, match="must include an SRI integrity value"):
        validate_candidate(payload, root, bundle)


def test_npm_lock_rejects_numeric_prerelease_with_leading_zero(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    lock_path = bundle / "locks/npm-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    package_path = next(path for path in lock["packages"] if path)
    lock["packages"][package_path]["version"] = "1.0.0-01"
    content = json.dumps(lock, sort_keys=True).encode("utf-8")
    _replace_lock(bundle, payload, "console-runtime", content)
    with pytest.raises(ManifestError, match="lacks a concrete semantic version"):
        validate_candidate(payload, root, bundle)


def test_accepted_decision_fails_closed_without_external_trust_anchor(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    with pytest.raises(ManifestError, match="no external trusted inventory baseline"):
        validate_candidate(payload, root, bundle)


def test_accepted_owner_cannot_override_failed_reconstruction_receipt(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    row = next(
        row for row in payload["lock_reconstructions"] if row["lock_id"] == "python-runtime-312"
    )
    old_digest = row["receipt"]["sha256"]
    _rewrite_receipt(bundle, payload, row, lambda receipt: receipt.__setitem__("outcome", "failed"))
    new_digest = row["receipt"]["sha256"]
    refs = payload["release_decision"]["evidence_refs"]
    refs[refs.index(old_digest)] = new_digest
    with pytest.raises(ManifestError, match="non-passing lock reconstruction"):
        validate_candidate(payload, root, bundle)


def test_unresolved_recovery_and_portfolio_roles_are_blocked_and_unacceptable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, {"recovery_ui", "portfolio"})
    validate_candidate(payload, root, bundle)
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    with pytest.raises(ManifestError, match="unresolved required component inventory roles"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    "reference",
    [
        "https://github.com/other/repo/issues/778",
        "https://github.com/MishkaStrategy/Module-Strategy/issues/not-a-number",
    ],
)
def test_blocked_by_requires_canonical_repository_issue_reference(
    tmp_path: Path, reference: str
) -> None:
    root, bundle, payload = _make_repo_case(
        tmp_path, {"recovery_ui"}, bad_dependency_reference=reference
    )
    with pytest.raises(ManifestError, match="source-pinned providers|url"):
        validate_candidate(payload, root, bundle)


def test_source_reference_bytes_must_match_checked_commit(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    original_check = release_manifest._clean_exact_source
    mutated = False

    def mutate_after_initial_clean(source_root: Path, commit: str) -> None:
        nonlocal mutated
        original_check(source_root, commit)
        if not mutated:
            mutated = True
            (source_root / "contracts/compatibility.json").write_text(
                "concurrent edit\n", encoding="utf-8"
            )

    monkeypatch.setattr(release_manifest, "_clean_exact_source", mutate_after_initial_clean)
    with pytest.raises(ManifestError, match="bytes differ from declared source commit"):
        validate_candidate(payload, root, bundle)


def test_schema_lookup_is_independent_of_process_cwd(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    monkeypatch.chdir(tmp_path)
    validate_candidate(payload, root, bundle)
    _accepted(payload, bundle)
    with pytest.raises(ManifestError, match="no external trusted inventory baseline"):
        validate_candidate(payload, root, bundle)


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
    with pytest.raises(ManifestError, match="duplicate JSON key"):
        load_manifest(path)


def test_nonfinite_json_number_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text('{"schema_version":NaN}', encoding="utf-8")
    with pytest.raises(ManifestError, match="nonfinite JSON value"):
        load_manifest(path)


@pytest.mark.parametrize(
    "mutation", ["missing_row", "extra_row", "duplicate_row", "wrong_set_identity"]
)
def test_results_registry_contract_inventory_is_pinned(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    contract_path = root / payload["candidate"]["compatibility"][0]["contract"]["path"]
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if mutation == "missing_row":
        contract["schemas"].pop()
    elif mutation == "extra_row":
        contract["schemas"].append(dict(contract["schemas"][-1], id="urn:synthetic:extra"))
    elif mutation == "duplicate_row":
        contract["schemas"].append(dict(contract["schemas"][0]))
    else:
        contract["schema_set_sha256"] = "0" * 64
    with pytest.raises(ManifestError, match="source-pinned Results Registry policy"):
        release_manifest._validate_results_registry_binding(
            payload["candidate"]["compatibility"][0]["contract"],
            json.dumps(contract).encode(),
            root,
            bundle,
            release_manifest._tracked_paths(root),
            payload["candidate"]["source"]["commit"],
        )


def test_results_registry_policy_pin_matches_both_schema_copies() -> None:
    policy = json.loads(
        Path("schemas/results-registry-compatibility-policy-v1.json").read_text(encoding="utf-8")
    )
    schema_id = "urn:modular-results-registry:strategy-audit-evidence-package:2"
    rows = [row for row in policy["schemas"] if row["id"] == schema_id]
    assert len(rows) == 1
    row = rows[0]
    source_bytes = Path(row["path"]).read_bytes()
    package_bytes = Path(row["package_path"]).read_bytes()

    assert source_bytes == package_bytes
    assert hashlib.sha256(source_bytes).hexdigest() == row["content_sha256"]
    schema_identity = sorted(
        (
            {
                "id": item["id"],
                "path": item["path"],
                "content_sha256": item["content_sha256"],
            }
            for item in policy["schemas"]
        ),
        key=lambda item: (item["id"], item["path"]),
    )
    assert (
        hashlib.sha256(release_manifest.canonical_json_bytes(schema_identity)).hexdigest()
        == policy["schema_set_sha256"]
    )


@pytest.mark.parametrize("mutation", ["missing_schema", "changed_schema", "missing_catalog"])
def test_results_registry_schema_files_must_match_bound_inventory(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    policy = json.loads(
        (root / "schemas/results-registry-compatibility-policy-v1.json").read_text()
    )
    target = root / (
        policy["source_catalog_path"]
        if mutation == "missing_catalog"
        else policy["schemas"][0]["package_path"]
    )
    if mutation == "changed_schema":
        target.write_bytes(target.read_bytes() + b" ")
    else:
        target.unlink()
    contract_ref = payload["candidate"]["compatibility"][0]["contract"]
    with pytest.raises(ManifestError):
        release_manifest._validate_results_registry_binding(
            contract_ref,
            (root / contract_ref["path"]).read_bytes(),
            root,
            bundle,
            release_manifest._tracked_paths(root),
            payload["candidate"]["source"]["commit"],
        )


def _registry_wheel_fixture(
    root: Path,
    bundle: Path,
    payload: dict[str, Any],
    mutation: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    policy = json.loads(
        (root / "schemas/results-registry-compatibility-policy-v1.json").read_text(encoding="utf-8")
    )
    members = [
        (
            PurePosixPath(row["package_path"])
            .relative_to("modules/results_aggregator/src")
            .as_posix(),
            (root / row["package_path"]).read_bytes(),
        )
        for row in policy["schemas"]
    ]
    if mutation == "missing":
        members.pop(0)
    elif mutation == "substituted":
        members[0] = (members[0][0], members[0][1] + b" ")
    elif mutation == "duplicate":
        members.append(members[0])
    elif mutation == "unsafe":
        members.append(("../escape.json", b"unsafe"))
    wheel_members = [(member_name, content, None) for member_name, content in members]
    filename, content = _build_wheel(
        "modular-results-registry", "0.1.0.dev0", extra_members=wheel_members
    )
    wheel_path = bundle / "dist" / filename
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    wheel_path.write_bytes(content)
    component = {
        "id": "modular-results-registry",
        "kind": "python_distribution",
        "version": "0.1.0.dev0",
    }
    return [
        {
            "component": component["id"],
            "kind": "distribution",
            "file": _ref(bundle, wheel_path, "bundle"),
        }
    ], {component["id"]: component}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "exactly one regular"),
        ("substituted", "schema bytes differ from source"),
        ("duplicate", "duplicate archive paths"),
        ("unsafe", "archive path"),
    ],
)
def test_results_registry_package_schemas_must_match_candidate_wheel(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, include_registry_pyproject=True)
    artifacts, components_by_id = _registry_wheel_fixture(root, bundle, payload, mutation)
    contract_ref = payload["candidate"]["compatibility"][0]["contract"]
    with pytest.raises(ManifestError, match=message):
        release_manifest._validate_results_registry_binding(
            contract_ref,
            (root / contract_ref["path"]).read_bytes(),
            root,
            bundle,
            release_manifest._tracked_paths(root),
            payload["candidate"]["source"]["commit"],
            candidate_artifacts=artifacts,
            components_by_id=components_by_id,
        )


def test_exact_results_registry_wheel_does_not_resolve_predecessor_material(
    tmp_path: Path,
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, include_registry_pyproject=True)
    artifacts, components_by_id = _registry_wheel_fixture(root, bundle, payload)
    contract_ref = payload["candidate"]["compatibility"][0]["contract"]
    release_manifest._validate_results_registry_binding(
        contract_ref,
        (root / contract_ref["path"]).read_bytes(),
        root,
        bundle,
        release_manifest._tracked_paths(root),
        payload["candidate"]["source"]["commit"],
        candidate_artifacts=artifacts,
        components_by_id=components_by_id,
    )
    row = _confirmed_compatibility_row(payload)
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(
        ManifestError,
        match="lacks resolved predecessor manifest and source/bundle-bound target contract/artifact",
    ):
        _validate_fixture_compatibility_coverage(root, payload, rows, accepted=True)


@pytest.mark.parametrize(
    "reference",
    [
        "#/$defs/does-not-exist",
        "urn:modular-results-registry:artifact-record:1#/$defs/does-not-exist",
        "https://example.invalid/unbound.schema.json",
        "../../missing/artifact-record.schema.json",
        "artifact-record.schema.json?unbound",
        "urn:modular-results-registry:artifact-record:1?unbound",
        "#fake",
        False,
    ],
)
def test_results_registry_dangling_and_external_refs_fail_closed(
    tmp_path: Path, reference: str
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, schema_ref_mutation=reference)
    with pytest.raises(ManifestError, match="Results Registry.*ref"):
        validate_candidate(payload, root, bundle)


def test_results_registry_nested_id_resource_fails_closed(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, schema_nested_id=True)
    with pytest.raises(ManifestError, match="nested Results Registry schema resource"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("schema_id_mismatch", "Results Registry schema \\$id mismatch"),
        ("schema_package_mismatch", "schema source/package bytes differ"),
        ("schema_extra_file", "catalog inventory does not match schema files"),
    ],
)
def test_results_registry_committed_inventory_tampering_reaches_binding_checks(
    tmp_path: Path, mutation: str, message: str
) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, **{mutation: True})
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_moved_results_registry_catalog_roots_are_policy_driven(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, moved_catalog_roots=True)
    validate_candidate(payload, root, bundle)


def test_free_text_assertion_cannot_extend_results_registry_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = json.loads(Path("schemas/results-registry-compatibility-policy-v1.json").read_text())
    policy_bytes = json.dumps(policy).encode()
    extra_bytes = json.dumps(
        {
            "contract_type": "compatibility_assertion_v1",
            "contract_version": 1,
            "component_id": "fixture-service",
            "assertion": "preserves API",
        }
    ).encode()
    monkeypatch.setattr(release_manifest, "_read_path", lambda *args, **kwargs: policy_bytes)
    monkeypatch.setattr(
        release_manifest,
        "_check_reference",
        lambda ref, *args, **kwargs: extra_bytes if ref.get("path") == "q" else policy_bytes,
    )
    monkeypatch.setattr(release_manifest, "_validate_results_registry_binding", lambda *args: None)
    compatibility = [
        {"id": "compat-v1", "contract": {"origin": "source", "path": "p", "sha256": "x"}},
        {"id": "extra", "contract": {"origin": "source", "path": "q", "sha256": "y"}},
    ]
    with pytest.raises(ManifestError, match="reserved for the separate Results Registry"):
        release_manifest._validate_compatibility_contracts(
            compatibility, tmp_path, tmp_path, set(), "0" * 40
        )


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_kind"])
def test_typed_results_registry_obligation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    policy = json.loads(Path("schemas/results-registry-compatibility-policy-v1.json").read_text())
    policy_bytes = json.dumps(policy).encode()
    wrong_bytes = json.dumps(
        {"contract_type": "compatibility_assertion_v1", "component_id": "other"}
    ).encode()
    monkeypatch.setattr(release_manifest, "_read_path", lambda *args, **kwargs: policy_bytes)
    monkeypatch.setattr(release_manifest, "_check_reference", lambda *args, **kwargs: wrong_bytes)
    monkeypatch.setattr(release_manifest, "_validate_results_registry_binding", lambda *args: None)
    row = {"id": "compat-v1", "contract": {"origin": "source", "path": "p", "sha256": "x"}}
    compatibility = (
        [] if mutation == "missing" else [row, dict(row)] if mutation == "duplicate" else [row]
    )
    with pytest.raises(ManifestError):
        release_manifest._validate_compatibility_contracts(
            compatibility, tmp_path, tmp_path, set(), "0" * 40
        )


def _confirmed_compatibility_row(payload: dict[str, Any]) -> dict[str, Any]:
    component = payload["candidate"]["components"][0]
    format_id = "urn:fixture:configuration:1"
    scopes = _fixture_compatibility_scopes(component["id"], payload)
    return {
        "component_id": component["id"],
        "status": "confirmed",
        "inventory_status": "enumerated",
        "owner": "fixture owner confirmation",
        "formats": [
            {
                "format_id": format_id,
                "format_kind": "configuration",
                "producers": [component["id"]],
                "consumers": [component["id"]],
                "relations": [
                    {
                        "scope": scope,
                        "predecessor": {
                            "release_id": "fixture-predecessor",
                            "candidate_id": "c" * 64,
                            "source_commit": "a" * 40,
                            "format_id": format_id,
                        },
                        "target": {
                            "source_commit": payload["candidate"]["source"]["commit"],
                            "component_id": component["id"],
                            "component_version": component["version"],
                            "descriptor_sha256": component["descriptor"]["sha256"],
                            "format_id": format_id,
                        },
                        "disposition": "unchanged-compatible",
                        "migration_id": None,
                        "rollback_id": None,
                    }
                    for scope in scopes
                ],
            }
        ],
    }


def _fixture_compatibility_scopes(
    component_id: str, payload: dict[str, Any]
) -> list[dict[str, str | None]]:
    rows = payload["support_profile"]["acceptance_requirements"]
    compatibility_gates = {
        row["component_id"]: row["gate"]
        for row in rows
        if row["gate"].startswith("compatibility:provider-")
    }
    components = {row["id"]: row for row in payload["candidate"]["components"]}
    return release_manifest._compatibility_scope_keys(
        component_id,
        components,
        payload["support_profile"],
        rows,
        compatibility_gates,
    )


def _attach_compatibility_subject_evidence(payload: dict[str, Any], row: dict[str, Any]) -> None:
    component_id = row["component_id"]
    coverage = next(
        item
        for item in payload["candidate"]["compatibility_coverage"]
        if item["component_id"] == component_id
    )
    coverage.update(row)
    subjects = release_manifest._compatibility_subjects(
        [row], {component_id: _fixture_compatibility_scopes(component_id, payload)}
    )
    template = next(
        item
        for item in payload["evidence"]
        if item["component_id"] == component_id
        and item["gate"].startswith("compatibility:provider-")
    )
    for digest, (_, scope) in subjects.items():
        evidence = dict(template)
        evidence["compatibility_subject_sha256"] = digest
        evidence["run_id"] = f"fixture-subject-{digest[:12]}"
        evidence["environment"] = scope["environment_id"]
        evidence["runtime_id"] = scope["runtime_id"]
        evidence["evidence_sha256"] = "0" * 64
        payload["evidence"].append(evidence)


def _validate_fixture_compatibility_coverage(
    root: Path, payload: dict[str, Any], rows: list[dict[str, Any]], *, accepted: bool = False
) -> None:
    inventory = json.loads(
        (root / payload["support_profile"]["component_inventory"]["path"]).read_text()
    )
    roles = inventory["roles"]
    gate_pairs = {
        (gate, provider)
        for role in roles
        for gate in role["required_gates"]
        if not gate.startswith(("lock:", "rebuild:"))
        for provider in role["providers"]
    }
    release_manifest._validate_compatibility_coverage(
        rows,
        roles,
        gate_pairs,
        payload["candidate"]["components"],
        payload["candidate"],
        {
            component["id"]: _fixture_compatibility_scopes(component["id"], payload)
            for component in payload["candidate"]["components"]
            if any(component["id"] in role["providers"] for role in roles)
        },
        accepted=accepted,
    )


@pytest.mark.parametrize("mutation", ["omission", "duplicate", "extra"])
def test_compatibility_coverage_must_match_source_pinned_providers(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, _, payload = repo_case
    rows = payload["candidate"]["compatibility_coverage"]
    if mutation == "omission":
        rows = rows[:-1]
    elif mutation == "duplicate":
        rows = [*rows, dict(rows[0])]
    else:
        rows = [*rows, {**rows[0], "component_id": "unlisted-extra"}]
    with pytest.raises(
        ManifestError, match="compatibility provider coverage|compatibility coverage"
    ):
        _validate_fixture_compatibility_coverage(root, payload, rows)


@pytest.mark.parametrize("mutation", ["missing_scope", "predecessor_identity", "disposition"])
def test_compatibility_schema_rejects_invalid_relation_identity_and_disposition(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    import jsonschema

    _, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    relation = row["formats"][0]["relations"][0]
    if mutation == "missing_scope":
        relation.pop("scope")
    elif mutation == "predecessor_identity":
        relation["predecessor"]["candidate_id"] = "not-a-candidate-id"
    else:
        relation["disposition"] = "compatible"
    manifest_schema = json.loads(
        Path("schemas/platform-release-manifest-v2.schema.json").read_text()
    )
    coverage_schema = {
        "$schema": manifest_schema["$schema"],
        "$defs": manifest_schema["$defs"],
        "$ref": "#/$defs/compatibilityCoverage",
    }
    errors = list(jsonschema.Draft202012Validator(coverage_schema).iter_errors(row))
    assert errors


@pytest.mark.parametrize(
    "mutation",
    ["missing_scope", "duplicate_scope", "out_of_profile_scope", "out_of_profile_runtime"],
)
def test_compatibility_relations_require_exact_profile_scopes(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    relations = row["formats"][0]["relations"]
    if mutation == "missing_scope":
        relations.pop()
        error = "missing a required environment/runtime scope"
    elif mutation == "duplicate_scope":
        relations.append(dict(relations[0]))
        error = "duplicate compatibility predecessor/target relation"
    elif mutation == "out_of_profile_scope":
        relations[0]["scope"]["environment_id"] = "unlisted-environment"
        error = "outside the support profile"
    else:
        relations[0]["scope"]["runtime_id"] = "unlisted-runtime"
        error = "outside the support profile"
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(ManifestError, match=error):
        _validate_fixture_compatibility_coverage(root, payload, rows)


@pytest.mark.parametrize(
    "mutation",
    ["source_commit", "component", "component_version", "descriptor", "format", "disposition"],
)
def test_confirmed_compatibility_relation_identity_and_evidence_fail_closed(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    relation = row["formats"][0]["relations"][0]
    if mutation == "source_commit":
        relation["target"]["source_commit"] = "f" * 40
    elif mutation == "component":
        relation["target"]["component_id"] = "unknown-component"
    elif mutation == "component_version":
        relation["target"]["component_version"] = "99.0"
    elif mutation == "descriptor":
        relation["target"]["descriptor_sha256"] = "f" * 64
    elif mutation == "format":
        relation["target"]["format_id"] = "urn:fixture:other:1"
    elif mutation == "disposition":
        relation["disposition"] = "compatible"
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(ManifestError):
        _validate_fixture_compatibility_coverage(root, payload, rows)


def test_accepted_decision_rejects_unknown_compatibility_relation(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    row["formats"][0]["relations"][0]["disposition"] = "unknown-blocking"
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(ManifestError, match="unknown compatibility relation"):
        _validate_fixture_compatibility_coverage(root, payload, rows, accepted=True)


def test_accepted_decision_rejects_unresolved_predecessor_material(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(
        ManifestError,
        match="lacks resolved predecessor manifest and source/bundle-bound target contract/artifact",
    ):
        _validate_fixture_compatibility_coverage(root, payload, rows, accepted=True)


@pytest.mark.parametrize(
    "mutation", ["missing_migration", "missing_rollback", "unrequired_rollback"]
)
def test_migrating_compatibility_relation_requires_declared_migration_and_rollback(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, _, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    relation = row["formats"][0]["relations"][0]
    migration = payload["candidate"]["migrations"][0]
    rollback = payload["candidate"]["rollback"][0]
    relation["predecessor"]["candidate_id"] = rollback["to_candidate"]
    migration["from"] = relation["predecessor"]["release_id"]
    migration["to"] = relation["target"]["component_version"]
    relation.update(disposition="migrate", migration_id=migration["id"], rollback_id=rollback["id"])
    rollback["from_candidate"] = "self"
    if mutation == "missing_migration":
        relation["migration_id"] = "missing-migration"
    elif mutation == "missing_rollback":
        relation["rollback_id"] = "missing-rollback"
    else:
        rollback["evidence_required"] = False
    rows = [row, *payload["candidate"]["compatibility_coverage"][1:]]
    with pytest.raises(ManifestError, match="migration and rollback"):
        _validate_fixture_compatibility_coverage(root, payload, rows)


def _pending_migration_relation(
    payload: dict[str, Any], *, from_candidate: str = "self"
) -> dict[str, Any]:
    row = _confirmed_compatibility_row(payload)
    migration = payload["candidate"]["migrations"][0]
    rollback = payload["candidate"]["rollback"][0]
    predecessor_id = "c" * 64
    migration["from"] = "fixture-predecessor"
    migration["to"] = row["formats"][0]["relations"][0]["target"]["component_version"]
    rollback["from_candidate"] = from_candidate
    rollback["to_candidate"] = predecessor_id
    for relation in row["formats"][0]["relations"]:
        relation["predecessor"]["candidate_id"] = predecessor_id
        relation.update(
            disposition="migrate", migration_id=migration["id"], rollback_id=rollback["id"]
        )
    _attach_compatibility_subject_evidence(payload, row)
    return row


def test_pending_manifest_migration_uses_acyclic_self_rollback_identity(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    _pending_migration_relation(payload)

    _refresh(payload, bundle)
    candidate_id = payload["candidate"]["candidate_id"]
    evidence_ids = [item["evidence_sha256"] for item in payload["evidence"]]
    envelope_id = release_manifest.manifest_sha256(payload)
    _refresh(payload, bundle)

    assert payload["candidate"]["rollback"][0]["from_candidate"] == "self"
    assert payload["candidate"]["candidate_id"] == candidate_id
    assert [item["evidence_sha256"] for item in payload["evidence"]] == evidence_ids
    assert release_manifest.manifest_sha256(payload) == envelope_id
    validate_candidate(payload, root, bundle)


def test_pending_migration_rejects_embedded_candidate_digest_in_rollback_from(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    embedded_candidate_id = payload["candidate"]["candidate_id"]
    _pending_migration_relation(payload, from_candidate=embedded_candidate_id)
    _refresh(payload, bundle)

    with pytest.raises(ManifestError, match="migration and rollback"):
        validate_candidate(payload, root, bundle)


def test_candidate_identity_is_stable_and_binds_rollback_content(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    _root, _bundle, payload = repo_case
    candidate = payload["candidate"]
    first = object_identity(candidate, "candidate_id")
    assert object_identity(candidate, "candidate_id") == first
    assert object_identity(candidate, "candidate_id") == first

    candidate["rollback"][0]["to_candidate"] = "f" * 64
    assert object_identity(candidate, "candidate_id") != first


def test_pending_manifest_accepts_provider_confirmed_format_with_scoped_evidence(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    row = _confirmed_compatibility_row(payload)
    component_id = row["component_id"]
    payload["candidate"]["compatibility_coverage"][0] = row
    required_scopes = {component_id: _fixture_compatibility_scopes(component_id, payload)}
    subjects = release_manifest._compatibility_subjects([row], required_scopes)
    template = next(
        item
        for item in payload["evidence"]
        if item["component_id"] == component_id
        and item["gate"].startswith("compatibility:provider-")
    )
    for digest, (_, scope) in subjects.items():
        evidence = dict(template)
        evidence["compatibility_subject_sha256"] = digest
        evidence["run_id"] = f"fixture-subject-{digest[:12]}"
        evidence["environment"] = scope["environment_id"]
        evidence["runtime_id"] = scope["runtime_id"]
        evidence["evidence_sha256"] = "0" * 64
        payload["evidence"].append(evidence)
    _refresh(payload, bundle)
    validate_candidate(payload, root, bundle)


def test_provider_compatibility_gates_are_mapped_to_profile_acceptance_pairs(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    rows = payload["support_profile"]["acceptance_requirements"]
    index = next(
        i for i, row in enumerate(rows) if row["gate"].startswith("compatibility:provider-")
    )
    rows.pop(index)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="no profile acceptance scope"):
        validate_candidate(payload, root, bundle)


def test_confirmed_compatibility_subject_requires_external_evidence_row(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["compatibility_coverage"][0] = _confirmed_compatibility_row(payload)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="confirmed compatibility subject requires"):
        validate_candidate(payload, root, bundle)


def test_accepted_decision_rejects_unknown_compatibility_coverage(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _, payload = repo_case
    with pytest.raises(ManifestError, match="unknown compatibility coverage"):
        _validate_fixture_compatibility_coverage(
            root, payload, payload["candidate"]["compatibility_coverage"], accepted=True
        )


def test_accepted_decision_rejects_unresolved_console_runtime_scope(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, _, payload = repo_case
    rows = [
        {
            "component_id": item["component_id"],
            "status": "confirmed",
            "inventory_status": "none",
            "owner": "fixture owner confirmation",
            "formats": [],
        }
        for item in payload["candidate"]["compatibility_coverage"]
    ]
    with pytest.raises(ManifestError, match="unresolved compatibility runtime scope"):
        _validate_fixture_compatibility_coverage(root, payload, rows, accepted=True)


@pytest.mark.parametrize("mutation", ["unknown", "candidate_id", "profile_id"])
def test_schema_and_identity_fail_closed(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    if mutation == "unknown":
        payload["extra"] = True
    elif mutation == "candidate_id":
        payload["candidate"]["candidate_id"] = "f" * 64
    else:
        payload["support_profile"]["profile_id"] = "f" * 64
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("mutation", ["omit_package", "duplicate_component", "duplicate_lock"])
def test_component_and_lock_inventory_fail_closed(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    if mutation == "omit_package":
        payload["candidate"]["components"].pop()
    elif mutation == "duplicate_component":
        payload["candidate"]["components"].append(dict(payload["candidate"]["components"][0]))
    else:
        payload["candidate"]["locks"].append(dict(payload["candidate"]["locks"][0]))
    _refresh(payload, bundle)
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


def test_console_omission_is_rejected(repo_case: tuple[Path, Path, dict[str, Any]]) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["components"] = [
        item for item in payload["candidate"]["components"] if item["kind"] != "web_console"
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="market-data-console"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("mutation", ["version", "descriptor_path", "descriptor_sha256"])
def test_component_metadata_must_match_source_package_metadata(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    component = payload["candidate"]["components"][0]
    if mutation == "version":
        component["version"] = "9.9.9"
    elif mutation == "descriptor_path":
        component["descriptor"]["path"] = "modules/01/pyproject.toml"
    else:
        component["descriptor"]["sha256"] = "f" * 64
    _refresh(payload, bundle)
    with pytest.raises(
        ManifestError,
        match="component metadata differs from package: fixture-distribution-00",
    ):
        validate_candidate(payload, root, bundle)


def test_untracked_component_source_is_rejected(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    source = root / "untracked-source.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    payload["candidate"]["components"][0]["source_files"] = [
        {
            "origin": "source",
            "path": "untracked-source.py",
            "sha256": sha256_bytes(source.read_bytes()),
        }
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="including untracked files"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("bad_path", ["../escape", "linked-source.py"])
def test_traversal_and_symlink_paths_are_rejected(
    repo_case: tuple[Path, Path, dict[str, Any]], bad_path: str
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["compatibility"][0]["contract"]["path"] = bad_path
    _refresh(payload, bundle)
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


def test_bundle_reference_rejects_symlink(repo_case: tuple[Path, Path, dict[str, Any]]) -> None:
    root, bundle, payload = repo_case
    artifact_path = bundle / payload["candidate"]["artifacts"][0]["file"]["path"]
    (bundle / "linked-artifact.whl").symlink_to(artifact_path)
    payload["candidate"]["artifacts"][0]["file"]["path"] = "linked-artifact.whl"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="symlink"):
        validate_candidate(payload, root, bundle)


def test_generic_bundle_artifact_digest_is_streamed(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    artifact = bundle / "large-console-artifact.bin"
    digest = hashlib.sha256()
    block = b"artifact-data" * 8192
    with artifact.open("wb") as stream:
        for _ in range(256):
            stream.write(block)
            digest.update(block)
    reference = {
        "origin": "bundle",
        "path": artifact.name,
        "sha256": digest.hexdigest(),
    }

    tracemalloc.start()
    try:
        release_manifest._verify_reference_digest(
            reference, "artifact", tmp_path, bundle, set(), "unused"
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert artifact.stat().st_size > 16 * 1024 * 1024
    assert peak < 4 * 1024 * 1024


def test_generic_bundle_artifact_digest_rejects_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    artifact = bundle / "console-artifact.bin"
    artifact.write_bytes(b"actual artifact bytes")
    reference = {"origin": "bundle", "path": artifact.name, "sha256": "0" * 64}

    with pytest.raises(ManifestError, match="SHA-256 does not match"):
        release_manifest._verify_reference_digest(
            reference, "artifact", tmp_path, bundle, set(), "unused"
        )


def test_bundle_must_cover_each_component_artifact(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["artifacts"].pop()
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="lacks artifacts"):
        validate_candidate(payload, root, bundle)


def test_lock_digest_mismatch_is_rejected(repo_case: tuple[Path, Path, dict[str, Any]]) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["locks"][0]["file"]["sha256"] = "f" * 64
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="SHA-256"):
        validate_candidate(payload, root, bundle)


def test_lock_ecosystem_must_match_component_kind(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["locks"][0]["ecosystem"] = "node"
    payload["candidate"]["locks"][0].pop("python_runtime_id")
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="Node lock"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("cross_environment", "is too long"),
        ("wrong_purpose", "must identify its structured purpose and runtime"),
        ("missing_tuple", "lack lock applicability or explicit no-lock classification"),
        ("ambiguous_tuple", "must identify its structured purpose and runtime"),
    ],
)
def test_lock_requirements_define_exact_applicability(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str, message: str
) -> None:
    root, bundle, payload = repo_case
    requirements = payload["support_profile"]["lock_requirements"]
    if mutation == "cross_environment":
        payload["support_profile"]["target_environments"].append("second-owner-environment")
        payload["support_profile"]["lock_requirements"].extend(
            {**row, "target_environment": "second-owner-environment"} for row in list(requirements)
        )
        _refresh(payload, bundle)
    elif mutation == "wrong_purpose":
        payload["candidate"]["locks"][0]["purpose"] = "test"
        _refresh(payload, bundle)
    elif mutation == "missing_tuple":
        requirements.pop()
        _refresh(payload, bundle)
    else:
        duplicate = dict(payload["candidate"]["locks"][0])
        duplicate["id"] = "same-applicability-different-lock"
        payload["candidate"]["locks"].append(duplicate)
        _refresh(payload, bundle)
    with pytest.raises(ManifestError, match=message):
        validate_candidate(payload, root, bundle)


def test_no_lock_classification_cannot_override_mandatory_python_locks(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["support_profile"]["no_lock_components"] = [
        {"component_id": payload["candidate"]["components"][0]["id"], "rationale": "declared"}
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="kind cannot be declared no-lock"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("mutation", ["missing", "failed", "cross_candidate"])
def test_accepted_evidence_must_be_passing_and_bound(
    repo_case: tuple[Path, Path, dict[str, Any]], mutation: str
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    if mutation == "missing":
        payload["evidence"].clear()
    elif mutation == "failed":
        payload["evidence"][0]["outcome"] = "failed"
        payload["evidence"][0]["evidence_sha256"] = object_identity(
            payload["evidence"][0], "evidence_sha256"
        )
        payload["release_decision"]["evidence_refs"][0] = payload["evidence"][0]["evidence_sha256"]
    else:
        payload["evidence"][0]["candidate_id"] = "f" * 64
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


def test_candidate_cannot_override_source_pinned_blocker(tmp_path: Path) -> None:
    root, bundle, payload = _make_repo_case(tmp_path, {"python_distributions"})
    payload["support_profile"]["capabilities"]["python_distributions"]["status"] = "required"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="must be blocked for unresolved required roles"):
        validate_candidate(payload, root, bundle)


def test_python_capability_must_cover_every_distribution(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["support_profile"]["capabilities"]["python_distributions"]["components"] = [
        payload["candidate"]["components"][0]["id"]
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="cover all 26"):
        validate_candidate(payload, root, bundle)


def test_component_inventory_shrinkage_is_rejected(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["components"].pop(0)
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="omits package"):
        validate_candidate(payload, root, bundle)


def test_capability_cannot_name_an_arbitrary_provider(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["support_profile"]["capabilities"]["operator_ui_control"]["components"] = [
        "@module-strategy/market-data-console"
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="providers differ from source-pinned inventory"):
        validate_candidate(payload, root, bundle)


def test_evidence_artifact_must_belong_to_evidence_component(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["evidence"][0]["component_id"] = "@module-strategy/market-data-console"
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="not bound to component"):
        validate_candidate(payload, root, bundle)


def test_accepted_decision_rejects_uncovered_required_gate(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    payload["evidence"] = [item for item in payload["evidence"] if item["gate"] != "gate_00"]
    _refresh(payload, bundle)
    with pytest.raises(
        ManifestError, match="typed wheel build evidence|gate/component/environment coverage"
    ):
        validate_candidate(payload, root, bundle)


def test_accepted_decision_refs_must_cover_required_gates(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    evidence = next(item for item in payload["evidence"] if item["gate"] == "gate_00")
    payload["release_decision"]["evidence_refs"].remove(evidence["evidence_sha256"])
    with pytest.raises(
        ManifestError, match="typed wheel build evidence|gate/component/environment coverage"
    ):
        validate_candidate(payload, root, bundle)


def test_results_registry_binding_cannot_be_candidate_selected_gate(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["candidate"]["compatibility"][0]["evidence_required"] = True
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="static validator obligation"):
        validate_candidate(payload, root, bundle)


def test_accepted_decision_requires_every_provider_gate_pair(
    repo_case: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    omitted = next(
        item
        for item in payload["evidence"]
        if item["gate"] == "gate_00" and item["component_id"] == "fixture-distribution-00"
    )
    payload["evidence"].remove(omitted)
    _refresh(payload, bundle)
    with pytest.raises(
        ManifestError, match="typed wheel build evidence|gate/component/environment coverage"
    ):
        validate_candidate(payload, root, bundle)


def test_support_profile_rejects_multiple_target_environments(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    environment = "second-owner-environment"
    payload["support_profile"]["target_environments"].append(environment)
    payload["support_profile"]["lock_requirements"].extend(
        {**row, "target_environment": environment}
        for row in list(payload["support_profile"]["lock_requirements"])
    )
    payload["candidate"]["locks"].extend(
        {**lock, "id": f"{lock['id']}-second-env", "target_environment": environment}
        for lock in list(payload["candidate"]["locks"])
    )
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="is too long"):
        validate_candidate(payload, root, bundle)


def test_schema_limits_support_profile_to_one_target_environment() -> None:
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/platform-release-manifest-v2.schema.json"
        ).read_text()
    )
    assert schema["$defs"]["profile"]["properties"]["target_environments"]["maxItems"] == 1


def test_python_lock_id_is_bound_to_its_runtime_minor_and_purpose(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    locks = payload["candidate"]["locks"]
    build = next(item for item in locks if item["id"] == "python-build-312")
    runtime = next(item for item in locks if item["id"] == "python-runtime-312")
    build["id"], runtime["id"] = runtime["id"], build["id"]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="must identify its structured purpose and runtime"):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("section", ["migrations", "rollback"])
def test_accepted_decision_requires_migration_and_rollback_declarations(
    repo_case: tuple[Path, Path, dict[str, Any]], section: str
) -> None:
    root, bundle, payload = repo_case
    _accepted(payload, bundle)
    removed = payload["candidate"][section]
    payload["candidate"][section] = []
    removed_gates = {f"{section[:-1]}:{item['id']}" for item in removed}
    payload["evidence"] = [
        item for item in payload["evidence"] if item["gate"] not in removed_gates
    ]
    _refresh(payload, bundle)
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


@pytest.mark.parametrize("section", ["migrations", "rollback"])
def test_accepted_decision_cannot_disable_migration_or_rollback_evidence(
    repo_case: tuple[Path, Path, dict[str, Any]], section: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bundle, payload = repo_case
    _allow_synthetic_console_runtime_scope(monkeypatch)
    _accepted(payload, bundle)
    payload["candidate"][section][0]["evidence_required"] = False
    _refresh(payload, bundle)
    with pytest.raises(ManifestError, match="must require evidence"):
        validate_candidate(payload, root, bundle)


def test_inconsistent_owner_decision_is_rejected(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    payload["release_decision"]["decision"] = "accepted"
    with pytest.raises(ManifestError):
        validate_candidate(payload, root, bundle)


def test_tracked_source_must_match_exact_clean_head(
    repo_case: tuple[Path, Path, dict[str, Any]],
) -> None:
    root, bundle, payload = repo_case
    (root / "modules/00/pyproject.toml").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="source tree must be clean"):
        validate_candidate(payload, root, bundle)
