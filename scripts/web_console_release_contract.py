"""Validate the structural web-console release sidecar for a v2 PAPER manifest.

This sidecar adds web build, packaging, serving and Darwin browser bindings. It
is intentionally subordinate to the v2 manifest validator: it cannot resolve
unknown-blocking scopes, authenticate evidence, or authorize a release.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import re
import subprocess
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from scripts import platform_release_manifest as platform_manifest

SCHEMA_PATH = Path("schemas/web-console-release-contract-v1.schema.json")
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
EXACT_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
INPUT_ROOT = "web/market-data-console/"
IGNORED_INPUT_PARTS = {"node_modules", "dist", "coverage", "test-results"}
MAX_WEB_CONSOLE_RECEIPT_BYTES = 8 * 1024 * 1024
_ZIP_MEMBER_CHUNK_BYTES = 64 * 1024
_BUNDLE_FILE_CHUNK_BYTES = 64 * 1024


class WebConsoleContractError(ValueError):
    """A malformed, mismatched, incomplete, or tampered web-console contract."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WebConsoleContractError(f"value cannot be canonicalized: {exc}") from exc


def object_sha256(value: dict[str, Any], identity_field: str) -> str:
    if identity_field not in value:
        raise WebConsoleContractError(f"missing identity field: {identity_field}")
    return hashlib.sha256(
        canonical_json_bytes({key: item for key, item in value.items() if key != identity_field})
    ).hexdigest()


def seal_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Set the canonical envelope digest after a fixture or draft is assembled."""
    contract["contract_sha256"] = object_sha256(contract, "contract_sha256")
    return contract


def _bindings(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        if set(value) == {"status", "value"} and value.get("status") in {"pending", "bound"}:
            return [path] if value["status"] == "pending" else []
        return [
            item
            for key, child in value.items()
            for item in _bindings(child, f"{path}.{key}".strip("."))
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _bindings(child, f"{path}[{index}]")
        ]
    return []


def _bound(contract: dict[str, Any], path: str) -> Any:
    current: Any = contract
    for part in path.split("."):
        current = current[part]
    if not isinstance(current, dict) or current.get("status") != "bound":
        raise WebConsoleContractError(f"accepted console contract has pending binding: {path}")
    return current["value"]


def _schema_validate(contract: dict[str, Any]) -> None:
    try:
        jsonschema = importlib.import_module("jsonschema")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = jsonschema.Draft202012Validator
        validator.check_schema(schema)
        errors = sorted(
            validator(schema, format_checker=jsonschema.FormatChecker()).iter_errors(contract),
            key=lambda row: list(row.absolute_path),
        )
    except (ImportError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WebConsoleContractError(f"cannot load web-console contract schema: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "contract"
        raise WebConsoleContractError(f"{location}: {error.message}")


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, str]:
    try:
        candidate = manifest["candidate"]
        profile = manifest["support_profile"]
        identity = {
            "manifest_sha256": hashlib.sha256(
                platform_manifest.canonical_json_bytes(manifest)
            ).hexdigest(),
            "candidate_id": candidate["candidate_id"],
            "profile_id": profile["profile_id"],
            "source_commit": candidate["source"]["commit"],
        }
    except (KeyError, TypeError) as exc:
        raise WebConsoleContractError(
            "linked v2 manifest lacks candidate/profile/source identity"
        ) from exc
    if manifest.get("schema_version") != 2:
        raise WebConsoleContractError("web-console contract must link to a v2 manifest")
    if not any(
        component.get("kind") == "web_console" for component in candidate.get("components", [])
    ):
        raise WebConsoleContractError("linked v2 manifest has no web-console component")
    return identity


def _read_bundle_file(
    bundle: Path,
    record: dict[str, Any],
    label: str,
    *,
    max_bytes: int | None = None,
) -> bytes:
    path = record.get("path")
    digest = record.get("sha256")
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise WebConsoleContractError(f"{label} path is invalid")
    posix = PurePosixPath(path)
    if (
        posix.is_absolute()
        or str(posix) != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise WebConsoleContractError(f"{label} path must be canonical and bundle-relative")
    if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
        raise WebConsoleContractError(f"{label} digest is invalid")
    target = bundle.joinpath(*posix.parts)
    byte_limit = MAX_WEB_CONSOLE_RECEIPT_BYTES if max_bytes is None else max_bytes
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(bundle)
        if not target.is_file() or target.is_symlink():
            raise WebConsoleContractError(f"{label} must be a regular non-symlink file")
        if resolved.stat().st_size > byte_limit:
            raise WebConsoleContractError(f"{label} exceeds the file-size limit")
        content_buffer = bytearray()
        with resolved.open("rb") as stream:
            while len(content_buffer) <= byte_limit:
                remaining = byte_limit + 1 - len(content_buffer)
                chunk = stream.read(min(_BUNDLE_FILE_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                content_buffer.extend(chunk)
        if len(content_buffer) > byte_limit:
            raise WebConsoleContractError(f"{label} exceeds the file-size limit")
        content = bytes(content_buffer)
    except WebConsoleContractError:
        raise
    except (OSError, ValueError) as exc:
        raise WebConsoleContractError(f"{label} is missing or escapes the bundle") from exc
    if hashlib.sha256(content).hexdigest() != digest:
        raise WebConsoleContractError(f"{label} digest mismatch")
    return content


def _tracked_console_paths(source: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-z", "--", INPUT_ROOT],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WebConsoleContractError(f"cannot enumerate tracked console inputs: {exc}") from exc
    paths = []
    for raw in result.stdout.decode("utf-8").split("\0"):
        if not raw:
            continue
        parts = PurePosixPath(raw).parts
        if not any(part in IGNORED_INPUT_PARTS for part in parts):
            paths.append(raw)
    return sorted(paths)


def _check_bound_contract(
    contract: dict[str, Any], manifest: dict[str, Any], source_root: Path, bundle_root: Path
) -> None:
    expected = _manifest_identity(manifest)
    if manifest["release_decision"].get("decision") != "accepted":
        raise WebConsoleContractError(
            "console sidecar cannot accept a pending or rejected v2 manifest"
        )
    for field, value in expected.items():
        if _bound(contract, f"manifest.{field}") != value:
            raise WebConsoleContractError(f"sidecar does not match linked v2 manifest {field}")

    node_version = _bound(contract, "toolchain.node_version")
    npm_version = _bound(contract, "toolchain.npm_version")
    if not isinstance(node_version, str) or EXACT_SEMVER.fullmatch(node_version) is None:
        raise WebConsoleContractError("Node version must be an exact stable X.Y.Z")
    if not isinstance(npm_version, str) or EXACT_SEMVER.fullmatch(npm_version) is None:
        raise WebConsoleContractError("npm version must be an exact stable X.Y.Z")
    provisioning = _bound(contract, "toolchain.provisioning_material")
    if (
        not isinstance(provisioning, dict)
        or not isinstance(provisioning.get("material_id"), str)
        or not isinstance(provisioning.get("source_uri"), str)
        or HEX_SHA256.fullmatch(str(provisioning.get("sha256", ""))) is None
    ):
        raise WebConsoleContractError("immutable Node/npm provisioning material is incomplete")
    source_url = urlsplit(provisioning["source_uri"])
    if (
        source_url.scheme not in {"https", "oci"}
        or not source_url.netloc
        or source_url.username
        or source_url.password
    ):
        raise WebConsoleContractError(
            "provisioning material source must be an immutable HTTPS/OCI reference"
        )
    registry = _bound(contract, "toolchain.registry_policy")
    if (
        not isinstance(registry, dict)
        or not isinstance(registry.get("registries"), list)
        or not registry["registries"]
        or not isinstance(registry.get("policy_id"), str)
    ):
        raise WebConsoleContractError("registry/source policy is incomplete")
    for url_value in registry["registries"]:
        parsed = urlsplit(url_value) if isinstance(url_value, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise WebConsoleContractError("registry policy contains an unsafe or invalid source")
    scripts = _bound(contract, "toolchain.install_script_policy")
    if (
        not isinstance(scripts, dict)
        or scripts.get("mode") not in {"disabled", "allowlist"}
        or not isinstance(scripts.get("allowlist"), list)
    ):
        raise WebConsoleContractError("npm lifecycle script policy is incomplete")
    if scripts["mode"] == "disabled" and scripts["allowlist"]:
        raise WebConsoleContractError("disabled npm lifecycle scripts cannot have an allowlist")
    if scripts["mode"] == "allowlist" and not scripts["allowlist"]:
        raise WebConsoleContractError("allowlisted npm lifecycle policy must name scripts")

    architecture = _bound(contract, "linux_build.architecture")
    image = _bound(contract, "linux_build.image")
    if not isinstance(architecture, str) or not architecture:
        raise WebConsoleContractError("Linux build architecture is missing")
    if (
        not isinstance(image, dict)
        or HEX_SHA256.fullmatch(str(image.get("sha256", ""))) is None
        or not isinstance(image.get("uri"), str)
        or "@sha256:" not in image["uri"]
    ):
        raise WebConsoleContractError("Linux build image must be immutably digest-pinned")
    recipe = _bound(contract, "linux_build.recipe")
    if (
        not isinstance(recipe, dict)
        or recipe.get("working_directory") != INPUT_ROOT.rstrip("/")
        or recipe.get("install_command") != ["npm", "ci"]
        or recipe.get("build_command") != ["npm", "run", "build"]
    ):
        raise WebConsoleContractError("Linux build recipe does not bind the declared console build")

    inputs = _bound(contract, "linux_build.source_inputs")
    if not isinstance(inputs, list):
        raise WebConsoleContractError("console source input manifest must be an array")
    input_paths = [row.get("path") for row in inputs if isinstance(row, dict)]
    if input_paths != _tracked_console_paths(source_root):
        raise WebConsoleContractError(
            "console source input manifest is incomplete, duplicated, or unordered"
        )
    source_commit = expected["source_commit"]
    tracked = platform_manifest._tracked_paths(source_root)
    for row in inputs:
        content = platform_manifest._read_path(
            "source",
            row["path"],
            "console source input",
            source_root,
            bundle_root,
            tracked,
            source_commit,
        )
        if hashlib.sha256(content).hexdigest() != row.get("sha256"):
            raise WebConsoleContractError(f"console source input digest mismatch: {row['path']}")
    package_path = f"{INPUT_ROOT}package.json"
    lock_path = f"{INPUT_ROOT}package-lock.json"
    by_path = {row["path"]: row for row in inputs}
    package_bytes = platform_manifest._read_path(
        "source",
        package_path,
        "console package metadata",
        source_root,
        bundle_root,
        tracked,
        source_commit,
    )
    lock_bytes = platform_manifest._read_path(
        "source", lock_path, "console npm lock", source_root, bundle_root, tracked, source_commit
    )
    relation = _bound(contract, "npm_lock.relation")
    if (
        not isinstance(relation, dict)
        or relation.get("resolver") != "npm"
        or relation.get("lockfile_version") != 3
        or relation.get("npm_version") != npm_version
    ):
        raise WebConsoleContractError("npm lock is not bound to the exact selected npm resolver")
    if (
        relation.get("package_json_sha256") != by_path[package_path]["sha256"]
        or relation.get("package_lock_sha256") != by_path[lock_path]["sha256"]
    ):
        raise WebConsoleContractError("npm lock relation package/lock digest mismatch")
    package = platform_manifest._strict_lock_json(package_bytes, "console package.json")
    declared_manager = package.get("packageManager", "absent")
    if relation.get("package_manager") != declared_manager:
        raise WebConsoleContractError("npm packageManager declaration differs from lock relation")
    platform_manifest._validate_npm_lock_v3(lock_bytes, package_bytes, "web console npm lock")

    receipt = _bound(contract, "linux_build.reconstruction_receipt")
    receipt_bytes = _read_bundle_file(bundle_root, receipt, "isolated npm reconstruction receipt")
    try:
        receipt_payload = json.loads(receipt_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebConsoleContractError("npm reconstruction receipt is not valid JSON") from exc
    if (
        not isinstance(receipt_payload, dict)
        or receipt_payload.get("outcome") != "passed"
        or receipt_payload.get("isolated") is not True
        or receipt_payload.get("source_commit") != source_commit
        or receipt_payload.get("npm_version") != npm_version
        or receipt_payload.get("node_version") != node_version
        or receipt_payload.get("lock_sha256") != relation["package_lock_sha256"]
    ):
        raise WebConsoleContractError(
            "npm reconstruction receipt does not match the bound build inputs"
        )

    artifact = _bound(contract, "linux_build.artifact")
    artifact_bytes = _read_bundle_file(
        bundle_root,
        artifact,
        "packaged web-console artifact",
        max_bytes=platform_manifest.MAX_WEB_ASSET_ARCHIVE_BYTES,
    )
    if artifact.get("format") != "zip" or not isinstance(artifact.get("members"), list):
        raise WebConsoleContractError(
            "packaged web-console artifact must declare a ZIP member manifest"
        )
    members = artifact["members"]
    if members != sorted(members, key=lambda row: row.get("path", "")) or len(
        {row.get("path") for row in members}
    ) != len(members):
        raise WebConsoleContractError("artifact member manifest must be sorted and unique")
    if hashlib.sha256(canonical_json_bytes(members)).hexdigest() != artifact.get(
        "member_manifest_sha256"
    ):
        raise WebConsoleContractError("artifact member manifest digest mismatch")
    try:
        platform_manifest._bounded_zip_entry_count(
            artifact_bytes,
            "packaged web-console artifact",
            max_entries=platform_manifest.MAX_WEB_ASSET_ARCHIVE_ENTRIES,
        )
        with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as archive:
            actual = []
            infos = archive.infolist()
            if len(infos) > platform_manifest.MAX_WEB_ASSET_ARCHIVE_ENTRIES:
                raise WebConsoleContractError(
                    "packaged web-console artifact exceeds the member-count limit"
                )
            declared_expanded = 0
            for info in infos:
                if info.file_size < 0 or info.compress_size < 0:
                    raise WebConsoleContractError(
                        "packaged web-console artifact has invalid member sizes"
                    )
                declared_expanded += info.file_size
                if declared_expanded > platform_manifest.MAX_WEB_ASSET_EXPANDED_BYTES:
                    raise WebConsoleContractError(
                        "packaged web-console artifact exceeds the expanded-size limit"
                    )
            actual_expanded = 0
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0o170000
                if (
                    path.is_absolute()
                    or str(path) != name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or info.is_dir()
                    or mode not in {0, 0o100000}
                    or info.date_time != (1980, 1, 1, 0, 0, 0)
                ):
                    raise WebConsoleContractError(
                        "artifact ZIP contains unsafe or nondeterministic members"
                    )
                member_digest = hashlib.sha256()
                member_size = 0
                with archive.open(info, "r") as member:
                    while chunk := member.read(_ZIP_MEMBER_CHUNK_BYTES):
                        member_size += len(chunk)
                        actual_expanded += len(chunk)
                        if member_size > info.file_size:
                            raise WebConsoleContractError(
                                "packaged artifact member exceeded its declared size"
                            )
                        if actual_expanded > platform_manifest.MAX_WEB_ASSET_EXPANDED_BYTES:
                            raise WebConsoleContractError(
                                "packaged web-console artifact exceeded the expanded-size limit"
                            )
                        member_digest.update(chunk)
                if member_size != info.file_size:
                    raise WebConsoleContractError(
                        "packaged artifact member size differs from its header"
                    )
                actual.append(
                    {
                        "path": name,
                        "size": member_size,
                        "sha256": member_digest.hexdigest(),
                    }
                )
    except platform_manifest.ManifestError as exc:
        raise WebConsoleContractError(
            "packaged web-console artifact ZIP is invalid or exceeds the member-count limit"
        ) from exc
    except (
        OSError,
        EOFError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise WebConsoleContractError("packaged web-console artifact is not a valid ZIP") from exc
    if actual != members:
        raise WebConsoleContractError("packaged artifact members differ from the declared manifest")

    mode = _bound(contract, "linux_serving.mode")
    if mode not in {"static_asset", "container", "managed_service"}:
        raise WebConsoleContractError(
            "Linux serving mode is not an explicit supported contract value"
        )
    runtime = _bound(contract, "linux_serving.runtime")
    if (
        not isinstance(runtime, dict)
        or not all(
            isinstance(runtime.get(key), str) and runtime[key]
            for key in ("name", "version", "image_sha256")
        )
        or HEX_SHA256.fullmatch(runtime["image_sha256"]) is None
    ):
        raise WebConsoleContractError("Linux serving runtime/image is incomplete")
    configuration = _bound(contract, "linux_serving.configuration")
    if not isinstance(configuration, list) or not configuration:
        raise WebConsoleContractError("Linux serving configuration must be explicitly hashed")
    for row in configuration:
        if row not in inputs:
            raise WebConsoleContractError(
                "serving configuration is not covered by source input hashes"
            )

    deployment = _bound(contract, "linux_deployment.context")
    if (
        not isinstance(deployment, dict)
        or deployment.get("os") != "linux"
        or deployment.get("architecture") != architecture
        or not all(
            isinstance(deployment.get(key), str) and deployment[key]
            for key in ("environment_id", "host_image_sha256", "serving_image_sha256")
        )
        or HEX_SHA256.fullmatch(deployment["host_image_sha256"]) is None
        or deployment["serving_image_sha256"] != runtime["image_sha256"]
    ):
        raise WebConsoleContractError(
            "Linux deployment context does not match the selected serving/build contract"
        )
    linux_evidence = _bound(contract, "linux_deployment.evidence")
    _validate_evidence(
        linux_evidence,
        "linux_deployment",
        deployment["environment_id"],
        artifact["sha256"],
        bundle_root,
    )

    darwin = _bound(contract, "darwin_acceptance.context")
    if (
        not isinstance(darwin, dict)
        or darwin.get("os") != "darwin"
        or not all(
            isinstance(darwin.get(key), str) and darwin[key]
            for key in ("environment_id", "architecture", "image_sha256")
        )
        or HEX_SHA256.fullmatch(darwin["image_sha256"]) is None
        or darwin["environment_id"] == deployment["environment_id"]
    ):
        raise WebConsoleContractError(
            "separate Darwin acceptance context is missing or overlaps Linux deployment"
        )
    browsers = _bound(contract, "darwin_acceptance.browser_contexts")
    if (
        not isinstance(browsers, list)
        or not browsers
        or any(
            not isinstance(row, dict)
            or row.get("environment_id") != darwin["environment_id"]
            or not all(
                isinstance(row.get(key), str) and row[key] for key in ("id", "browser", "version")
            )
            for row in browsers
        )
    ):
        raise WebConsoleContractError(
            "Darwin browser acceptance contexts are missing or mismatched"
        )
    if len({row["id"] for row in browsers}) != len(browsers):
        raise WebConsoleContractError("Darwin browser context identities must be unique")

    artifact_use = _bound(contract, "darwin_acceptance.artifact_use")
    if not isinstance(artifact_use, dict) or artifact_use.get("mode") not in {
        "same_artifact",
        "separate_rebuild",
    }:
        raise WebConsoleContractError("Darwin artifact consumption mode must be explicit")
    if artifact_use["mode"] == "same_artifact":
        if artifact_use.get("artifact_sha256") != artifact["sha256"]:
            raise WebConsoleContractError(
                "Darwin acceptance substituted an artifact for the Linux artifact"
            )
    elif (
        artifact_use.get("source_commit") != source_commit
        or not isinstance(artifact_use.get("artifact_sha256"), str)
        or HEX_SHA256.fullmatch(artifact_use["artifact_sha256"]) is None
    ):
        raise WebConsoleContractError(
            "separate Darwin rebuild is not bound to the same source commit"
        )
    darwin_evidence = _bound(contract, "darwin_acceptance.evidence")
    _validate_evidence(
        darwin_evidence,
        "darwin_acceptance",
        darwin["environment_id"],
        artifact_use["artifact_sha256"],
        bundle_root,
    )
    if (
        linux_evidence["evidence_id"] == darwin_evidence["evidence_id"]
        or linux_evidence["receipt"]["sha256"] == darwin_evidence["receipt"]["sha256"]
    ):
        raise WebConsoleContractError(
            "Linux deployment evidence cannot substitute for Darwin acceptance evidence"
        )


def _validate_evidence(
    evidence: Any, role: str, context_id: str, artifact_sha256: str, bundle: Path
) -> None:
    if (
        not isinstance(evidence, dict)
        or evidence.get("role") != role
        or evidence.get("context_id") != context_id
        or evidence.get("artifact_sha256") != artifact_sha256
        or evidence.get("outcome") != "passed"
        or not isinstance(evidence.get("evidence_id"), str)
        or not evidence["evidence_id"]
    ):
        raise WebConsoleContractError(f"{role} evidence is not bound to its context and artifact")
    content = _read_bundle_file(bundle, evidence.get("receipt", {}), f"{role} evidence receipt")
    if not content:
        raise WebConsoleContractError(f"{role} evidence receipt is empty")


def validate_contract(
    contract: dict[str, Any],
    manifest: dict[str, Any],
    source_root: str | Path,
    bundle_root: str | Path,
) -> None:
    """Verify a sidecar draft, or verify all bindings before structural acceptance.

    A pending sidecar is an auditable blocker list. An accepted sidecar is only
    a structural supplement and cannot make the linked v2 manifest acceptable.
    """
    if not isinstance(contract, dict):
        raise WebConsoleContractError("web-console sidecar root must be an object")
    _schema_validate(contract)
    if contract["contract_sha256"] != object_sha256(contract, "contract_sha256"):
        raise WebConsoleContractError("web-console contract digest mismatch")
    _manifest_identity(manifest)
    pending = sorted(
        _bindings(
            {
                key: value
                for key, value in contract.items()
                if key not in {"contract_sha256", "blockers"}
            }
        )
    )
    if contract["disposition"] == "pending":
        if not pending or contract["blockers"] != pending:
            raise WebConsoleContractError(
                "pending draft blockers do not exactly enumerate unresolved bindings"
            )
        for field, actual in _manifest_identity(manifest).items():
            binding = contract["manifest"][field]
            if binding["status"] == "bound" and binding["value"] != actual:
                raise WebConsoleContractError(
                    f"pending sidecar does not match linked v2 manifest {field}"
                )
        return
    if pending or contract["blockers"]:
        raise WebConsoleContractError(
            "accepted console contract must bind every field and have no blockers"
        )
    if contract["contract_sha256"] != object_sha256(contract, "contract_sha256"):
        raise WebConsoleContractError("web-console contract digest mismatch")
    _check_bound_contract(
        contract,
        manifest,
        Path(source_root).resolve(strict=True),
        Path(bundle_root).resolve(strict=True),
    )


def validate_combined_candidate(
    manifest: dict[str, Any],
    contract: dict[str, Any] | None,
    source_root: str | Path,
    bundle_root: str | Path,
) -> None:
    """Require a verified sidecar before v2 candidate validation can proceed.

    The v2 validator remains authoritative and still applies its trust/evidence
    gates. This wrapper never promotes a pending or unknown-blocking v2 scope.
    """
    if contract is None:
        raise WebConsoleContractError(
            "combined candidate validation requires the v1 web-console sidecar"
        )
    validate_contract(contract, manifest, source_root, bundle_root)
    platform_manifest.validate_candidate(manifest, source_root, bundle_root)
