"""Structurally validate platform release manifest v2 candidates.

This candidate-local validator proves structure only; it does not authenticate
owner approval or make a trusted release decision. Embedded evidence digests
cover manifest records, not external CI payload bytes. Trusted acceptance
requires a verifier, schema, and policy invoked outside candidate-controlled
code from a protected, independently pinned identity.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import importlib
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tomllib
import zipfile
import zlib
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.markers import Variable
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    canonicalize_version,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.tags import parse_tag
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from scripts.release_obligation_report import ObligationReport

SCHEMA_PATH = Path("schemas/platform-release-manifest-v2.schema.json")
V3_SCHEMA_PATH = Path("schemas/platform-release-manifest-v3.schema.json")
CANONICAL_REPOSITORY = "https://github.com/MishkaStrategy/Module-Strategy.git"
IDENTITY_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
DEPENDENCY_ISSUE_RE = re.compile(
    r"^https://github\.com/MishkaStrategy/Module-Strategy/issues/[1-9][0-9]*$"
)
GATE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
NPM_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
SHA256_REQUIREMENT_HASH_RE = re.compile(r"\s--hash=sha256:([a-fA-F0-9]{64})(?=\s|$)")
SRI_TOKEN_RE = re.compile(r"^(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})$")
MAX_DISTRIBUTION_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_DISTRIBUTION_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_DISTRIBUTION_ARCHIVE_ENTRIES = 100_000
MAX_DISTRIBUTION_METADATA_BYTES = 8 * 1024 * 1024
MAX_WEB_ASSET_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_WEB_ASSET_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_WEB_ASSET_ARCHIVE_ENTRIES = 100_000
ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"


class ManifestError(ValueError):
    """Manifest or repository validation failed."""


@dataclass(frozen=True, slots=True)
class _ManifestPolicy:
    schema_version: int
    schema_path: Path
    schema_error_label: str


_V2_POLICY = _ManifestPolicy(2, SCHEMA_PATH, "v2")
_V3_POLICY = _ManifestPolicy(3, V3_SCHEMA_PATH, "v3")

_V3_SELECTED_PYTHON_EXTRAS = {
    "modular-strategy-core": ("backtest", "signal-bot"),
    "modular-market-data-ingestion": ("interface", "parquet"),
    "modular-control-plane": ("interface",),
    "modular-system-acceptance": ("package-runtime-owner",),
}


def _canonical_v3_python_dependency_selection(provider_ids: list[str]) -> dict[str, Any]:
    """Build the sole v3 profile's canonical Python provider selection rows."""
    return {
        "schema_version": 1,
        "providers": [
            {
                "component_id": component_id,
                "extras": list(_V3_SELECTED_PYTHON_EXTRAS.get(component_id, ())),
            }
            for component_id in sorted(provider_ids)
        ],
    }


_MANIFEST_POLICIES = MappingProxyType({2: _V2_POLICY, 3: _V3_POLICY})


def _policy_for_version(value: Any) -> _ManifestPolicy:
    # Preserve the legacy v2 schema checkpoint for every value except exact
    # integer 3. Invalid, absent, and future versions must fail its closed
    # schema exactly where they did before this dispatcher existed.
    if type(value) is int and value == 3:
        return _V3_POLICY
    return _V2_POLICY


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ManifestError(f"nonfinite JSON value is forbidden: {value}")


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load UTF-8 JSON, rejecting duplicate keys and nonfinite numbers."""
    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    return payload


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes for identity and digest use."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"value cannot be canonicalized: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_identity(value: dict[str, Any], id_field: str) -> str:
    """Hash an object without its self-referential identity field."""
    if id_field not in value:
        raise ManifestError(f"missing identity field: {id_field}")
    body = {key: item for key, item in value.items() if key != id_field}
    return sha256_bytes(canonical_json_bytes(body))


def manifest_sha256(payload: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def _schema_validate(
    payload: dict[str, Any],
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
    policy: _ManifestPolicy = _V2_POLICY,
) -> str:
    try:
        jsonschema = importlib.import_module("jsonschema")
    except ImportError as exc:
        raise ManifestError(
            "jsonschema is required; install the 'release-validation' optional extra"
        ) from exc
    try:
        schema_bytes = _read_path(
            "source",
            policy.schema_path.as_posix(),
            "manifest schema",
            source_root,
            candidate_bundle_root,
            tracked,
            source_commit,
        )
        schema = json.loads(schema_bytes.decode("utf-8"))
        validator_type = jsonschema.Draft202012Validator
        validator_type.check_schema(schema)
        errors = sorted(
            validator_type(schema, format_checker=jsonschema.FormatChecker()).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load {policy.schema_error_label} schema: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "manifest"
        raise ManifestError(f"{location}: {error.message}")
    return sha256_bytes(schema_bytes)


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ManifestError(f"git command failed: {exc}") from exc
    return result.stdout.strip()


def _tracked_paths(root: Path) -> set[str]:
    return set(_git(root, "ls-files", "-z").split("\0")) - {""}


def _read_path(
    origin: str,
    raw_path: Any,
    label: str,
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
    max_bytes: int | None = None,
    hash_only: bool = False,
) -> bytes | str:
    if origin == "source":
        if hash_only:
            raise ManifestError(f"{label} digest-only reads require a bundle reference")
        root = source_root
    elif origin == "bundle":
        root = candidate_bundle_root
    else:
        raise ManifestError(f"{label} origin must be source or bundle")
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise ManifestError(f"{label} path is invalid")
    posix = PurePosixPath(raw_path)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in raw_path.split("/")):
        raise ManifestError(f"{label} path must be canonical and repository-relative")
    if str(posix) != raw_path:
        raise ManifestError(f"{label} path must be canonical and repository-relative")
    if origin == "source" and raw_path not in tracked:
        raise ManifestError(f"{label} path is not a tracked repository path")
    if origin == "source":
        tree_entry = _git(
            source_root, "--literal-pathspecs", "ls-tree", "-z", source_commit, "--", raw_path
        )
        try:
            metadata, tree_path = tree_entry.rstrip("\0").split("\t", 1)
            mode, object_type, _object_id = metadata.split(" ", 2)
        except ValueError as exc:
            raise ManifestError(f"{label} is absent from declared source commit") from exc
        if object_type != "blob" or mode not in {"100644", "100755"} or tree_path != raw_path:
            raise ManifestError(f"{label} is not a regular file in declared source commit")
    if not getattr(os, "O_NOFOLLOW", 0) or os.open not in os.supports_dir_fd:
        raise ManifestError("platform cannot safely open no-follow file references")
    try:
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise ManifestError(f"{label} root is unavailable") from exc
    file_fd = -1
    try:
        for part in posix.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            posix.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
        )
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ManifestError(f"{label} must refer to a regular file")
        if max_bytes is not None and file_stat.st_size > max_bytes:
            raise ManifestError(f"{label} exceeds the compressed-size limit")
        chunks: list[bytes] = []
        digest = hashlib.sha256() if hash_only else None
        total_bytes = 0
        while chunk := os.read(file_fd, 1024 * 1024):
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise ManifestError(f"{label} exceeds the compressed-size limit")
            if digest is None:
                chunks.append(chunk)
            else:
                digest.update(chunk)
        content: bytes | str = digest.hexdigest() if digest is not None else b"".join(chunks)
    except OSError as exc:
        raise ManifestError(f"{label} path is missing or traverses a symlink") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)
    if origin == "source" and not hash_only:
        committed_content = subprocess.run(
            ["git", "-C", str(source_root), "cat-file", "blob", f"{source_commit}:{raw_path}"],
            check=True,
            capture_output=True,
        ).stdout
        if content != committed_content:
            raise ManifestError(f"{label} bytes differ from declared source commit")
    return content


def _read_reference(
    reference: Any,
    label: str,
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
    max_bytes: int | None = None,
) -> bytes:
    if not isinstance(reference, dict):
        raise ManifestError(f"{label} reference must be an object")
    origin = reference.get("origin")
    path = reference.get("path")
    if not isinstance(origin, str) or not isinstance(path, str):
        raise ManifestError(f"{label} reference origin and path must be strings")
    content = _read_path(
        origin,
        path,
        label,
        source_root,
        candidate_bundle_root,
        tracked,
        source_commit,
        max_bytes,
    )
    if not isinstance(content, bytes):
        raise ManifestError(f"{label} content read failed")
    if sha256_bytes(content) != reference.get("sha256"):
        raise ManifestError(f"{label} SHA-256 does not match")
    return content


def _verify_reference_digest(
    reference: Any,
    label: str,
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
) -> None:
    """Verify a bundle reference without retaining its bytes in memory."""
    if not isinstance(reference, dict):
        raise ManifestError(f"{label} reference must be an object")
    origin = reference.get("origin")
    path = reference.get("path")
    if not isinstance(origin, str) or not isinstance(path, str):
        raise ManifestError(f"{label} reference origin and path must be strings")
    digest = _read_path(
        origin,
        path,
        label,
        source_root,
        candidate_bundle_root,
        tracked,
        source_commit,
        hash_only=True,
    )
    if not isinstance(digest, str):
        raise ManifestError(f"{label} digest read failed")
    if digest != reference.get("sha256"):
        raise ManifestError(f"{label} SHA-256 does not match")


def _strict_json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return value


def _validate_results_registry_binding(
    contract_ref: dict[str, Any],
    contract_bytes: bytes,
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    *,
    candidate_artifacts: list[dict[str, Any]] | None = None,
    components_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Validate the explicitly typed, source-pinned Results Registry contract."""
    policy_path = "schemas/results-registry-compatibility-policy-v1.json"
    policy_bytes = _read_path(
        "source",
        policy_path,
        "Results Registry compatibility policy",
        source,
        bundle,
        tracked,
        source_commit,
    )
    policy = _strict_json_object(policy_bytes, "Results Registry compatibility policy")
    contract = _strict_json_object(contract_bytes, "compatibility contract")
    if (
        contract != policy
        or contract.get("contract_type") != "results_registry_schema_binding"
        or contract.get("component_id") != "modular-results-registry"
    ):
        raise ManifestError(
            "compatibility contract does not match source-pinned Results Registry policy"
        )
    rows = policy.get("schemas")
    if not isinstance(rows, list) or not rows:
        raise ManifestError("Results Registry policy schema inventory is invalid")
    source_catalog_path = policy.get("source_catalog_path")
    package_catalog_path = policy.get("package_catalog_path")
    source_catalog = _strict_json_object(
        _read_path(
            "source",
            source_catalog_path,
            "Results Registry source catalog",
            source,
            bundle,
            tracked,
            source_commit,
        ),
        "Results Registry source catalog",
    )
    package_catalog = _strict_json_object(
        _read_path(
            "source",
            package_catalog_path,
            "Results Registry packaged catalog",
            source,
            bundle,
            tracked,
            source_commit,
        ),
        "Results Registry packaged catalog",
    )
    if source_catalog != package_catalog:
        raise ManifestError("Results Registry source and packaged catalogs differ")
    entries = source_catalog.get("schemas")
    if (
        source_catalog.get("schema_version") != "1"
        or not isinstance(entries, list)
        or source_catalog.get("catalog_hash") != policy.get("catalog_hash")
        or sha256_bytes(
            canonical_json_bytes(
                {"schema_version": source_catalog.get("schema_version"), "schemas": entries}
            )
        )
        != source_catalog.get("catalog_hash")
    ):
        raise ManifestError("Results Registry catalog hash or version is invalid")
    if entries != [{"id": row.get("id"), "path": Path(row.get("path", "")).name} for row in rows]:
        raise ManifestError(
            "Results Registry catalog inventory does not match source-pinned policy"
        )
    ids = [row.get("id") for row in rows]
    paths = [row.get("path") for row in rows]
    package_paths = [row.get("package_path") for row in rows]
    if (
        ids != sorted(ids)
        or paths != sorted(paths)
        or len(ids) != len(set(ids))
        or len(paths) != len(set(paths))
        or len(package_paths) != len(set(package_paths))
    ):
        raise ManifestError("Results Registry schema IDs and paths must be unique and sorted")
    source_dir = _catalog_root(source_catalog_path)
    package_dir = _catalog_root(package_catalog_path)
    component_id = contract.get("component_id")
    bind_candidate_wheel = (
        candidate_artifacts is not None
        and components_by_id is not None
        and component_id in components_by_id
    )
    wheel_schema_bytes: dict[str, bytes] = {}
    if bind_candidate_wheel:
        package_catalog_parts = PurePosixPath(package_catalog_path).parts
        src_indexes = [index for index, part in enumerate(package_catalog_parts) if part == "src"]
        if len(src_indexes) != 1 or src_indexes[0] + 1 >= len(package_catalog_parts):
            raise ManifestError(
                "Results Registry packaged catalog is not in a supported src layout"
            )
        src_index = src_indexes[0]
        package_src_root = PurePosixPath(*package_catalog_parts[: src_index + 1])
        package_name = package_catalog_parts[src_index + 1]
        project_root = PurePosixPath(*package_catalog_parts[:src_index])
        pyproject_path = (project_root / "pyproject.toml").as_posix()
        pyproject_bytes = _read_path(
            "source",
            pyproject_path,
            "Results Registry package build configuration",
            source,
            bundle,
            tracked,
            source_commit,
        )
        try:
            package_pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError("Results Registry package build configuration is invalid") from exc
        setuptools_config = package_pyproject.get("tool", {}).get("setuptools", {})
        package_data = setuptools_config.get("package-data", {}).get(package_name)
        package_project_name = package_pyproject.get("project", {}).get("name")
        if (
            not isinstance(package_project_name, str)
            or canonicalize_name(package_project_name) != canonicalize_name(component_id)
            or "package-dir" in setuptools_config
            or not isinstance(package_data, list)
            or not package_data
        ):
            raise ManifestError("Results Registry package build layout is unsupported")
    for row in rows:
        if not _path_within(row.get("path"), source_dir) or not _path_within(
            row.get("package_path"), package_dir
        ):
            raise ManifestError("Results Registry schema path is outside its catalog root")
    expected_names = {PurePosixPath(path).relative_to(source_dir).as_posix() for path in paths}
    expected_package_names = {
        PurePosixPath(path).relative_to(package_dir).as_posix() for path in package_paths
    }
    for root_path, declared_names, catalog_name in (
        (
            source.joinpath(*source_dir.parts),
            expected_names,
            PurePosixPath(source_catalog_path).name,
        ),
        (
            source.joinpath(*package_dir.parts),
            expected_package_names,
            PurePosixPath(package_catalog_path).name,
        ),
    ):
        names = {item.relative_to(root_path).as_posix() for item in root_path.rglob("*.json")}
        expected = declared_names | {catalog_name}
        if names != expected:
            raise ManifestError("Results Registry catalog inventory does not match schema files")
    for row in rows:
        identifier, path, package_path = row.get("id"), row.get("path"), row.get("package_path")
        if not all(isinstance(value, str) and value for value in (identifier, path, package_path)):
            raise ManifestError("Results Registry schema policy row is invalid")
        source_bytes = _read_path(
            "source",
            path,
            f"Results Registry schema {identifier}",
            source,
            bundle,
            tracked,
            source_commit,
        )
        packaged_bytes = _read_path(
            "source",
            package_path,
            f"Results Registry packaged schema {identifier}",
            source,
            bundle,
            tracked,
            source_commit,
        )
        if source_bytes != packaged_bytes:
            raise ManifestError(
                f"Results Registry schema source/package bytes differ: {identifier}"
            )
        if sha256_bytes(source_bytes) != row.get("content_sha256"):
            raise ManifestError(f"Results Registry schema content hash mismatch: {identifier}")
        if bind_candidate_wheel:
            package_parts = PurePosixPath(package_path).parts
            if (
                package_parts[: src_index + 1] != package_src_root.parts
                or len(package_parts) <= src_index + 1
            ):
                raise ManifestError(
                    "Results Registry package schema is outside the declared src root"
                )
            if package_parts[src_index + 1] != package_name:
                raise ManifestError("Results Registry package schema belongs to another package")
            member_parts = package_parts[src_index + 2 :]
            member_relative = PurePosixPath(*member_parts).as_posix()
            if not any(
                isinstance(pattern, str) and PurePosixPath(member_relative).match(pattern)
                for pattern in package_data
            ):
                raise ManifestError("Results Registry schema is not declared as package data")
            wheel_schema_bytes[PurePosixPath(package_name, *member_parts).as_posix()] = (
                packaged_bytes
            )
        document = _strict_json_object(source_bytes, f"Results Registry schema {identifier}")
        if document.get("$id") != identifier:
            raise ManifestError(f"Results Registry schema $id mismatch: {path}")
    schema_identity = [
        {"id": row["id"], "path": row["path"], "content_sha256": row["content_sha256"]}
        for row in rows
    ]
    schema_identity.sort(key=lambda item: (item["id"], item["path"]))
    if sha256_bytes(canonical_json_bytes(schema_identity)) != policy.get("schema_set_sha256"):
        raise ManifestError("Results Registry schema-set identity mismatch")
    # Resolve every ref locally and fail closed for all external/network references.
    by_id = {row["id"]: row for row in rows}
    documents = {
        row["id"]: _strict_json_object(
            _read_path(
                "source", row["path"], f"schema {row['id']}", source, bundle, tracked, source_commit
            ),
            f"schema {row['id']}",
        )
        for row in rows
    }
    path_to_id = {row["path"]: row["id"] for row in rows}
    path_to_id.update({row["package_path"]: row["id"] for row in rows})
    for current_id, document in documents.items():

        def refs(value: Any, *, root_document: bool = False) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    ref = value["$ref"]
                    if not isinstance(ref, str):
                        raise ManifestError(
                            f"Results Registry $ref must be a string in {current_id}"
                        )
                    _resolve_registry_ref(ref, current_id, documents, by_id, path_to_id)
                if (
                    "$anchor" in value
                    or "$dynamicAnchor" in value
                    or ("$id" in value and not root_document)
                ):
                    raise ManifestError(
                        f"unsupported nested Results Registry schema resource in {current_id}"
                    )
                for item in value.values():
                    refs(item)
            elif isinstance(value, list):
                for item in value:
                    refs(item)

        refs(document, root_document=True)

    if bind_candidate_wheel:
        component = components_by_id[component_id]
        if component["kind"] != "python_distribution":
            raise ManifestError("Results Registry target component is not a Python distribution")
        wheels = [
            artifact
            for artifact in candidate_artifacts
            if artifact.get("component") == component_id
            and artifact.get("kind") == "distribution"
            and artifact.get("file", {}).get("path", "").endswith(".whl")
        ]
        if not wheels:
            raise ManifestError("Results Registry candidate lacks a declared wheel artifact")
        for artifact in wheels:
            if artifact["file"].get("origin") != "bundle":
                raise ManifestError(
                    "Results Registry target wheel must originate in candidate bundle"
                )
            artifact_bytes = _read_reference(
                artifact["file"],
                "Results Registry candidate wheel",
                source,
                bundle,
                tracked,
                source_commit,
                MAX_DISTRIBUTION_ARCHIVE_BYTES,
            )
            artifact_format = _validate_python_distribution_artifact(
                artifact["file"]["path"], artifact_bytes, component
            )
            if artifact_format != "wheel":
                raise ManifestError("Results Registry target artifact is not a wheel")
            _validate_results_registry_wheel_members(
                artifact_bytes,
                f"Results Registry wheel for {component_id}",
                wheel_schema_bytes,
            )


def _validate_results_registry_wheel_members(
    content: bytes, label: str, expected_members: dict[str, bytes]
) -> None:
    """Bind source-pinned Results Registry schemas to exact wheel members."""
    _read_wheel_metadata(content, label)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos_by_name: dict[str, list[zipfile.ZipInfo]] = {}
            for info in archive.infolist():
                infos_by_name.setdefault(info.filename, []).append(info)
            for member_name, expected_bytes in expected_members.items():
                matches = infos_by_name.get(member_name, [])
                if len(matches) != 1 or matches[0].is_dir():
                    raise ManifestError(
                        f"{label} must contain exactly one regular {member_name} member"
                    )
                actual_bytes = archive.read(matches[0])
                if len(actual_bytes) != matches[0].file_size or actual_bytes != expected_bytes:
                    raise ManifestError(f"{label} schema bytes differ from source: {member_name}")
    except ManifestError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ManifestError(f"{label} is not a valid bounded wheel archive") from exc


def _validate_compatibility_contracts(
    compatibility: list[dict[str, Any]],
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    *,
    candidate_artifacts: list[dict[str, Any]] | None = None,
    components_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    policy_path = "schemas/results-registry-compatibility-policy-v1.json"
    policy = _strict_json_object(
        _read_path(
            "source", policy_path, "compatibility policy", source, bundle, tracked, source_commit
        ),
        "compatibility policy",
    )
    obligation = policy.get("required_obligation")
    if not isinstance(obligation, dict) or set(obligation) != {
        "compatibility_id",
        "contract_type",
        "component_id",
    }:
        raise ManifestError("source-pinned Results Registry compatibility obligation is invalid")
    required_id = obligation["compatibility_id"]
    required_rows = [item for item in compatibility if item.get("id") == required_id]
    if len(required_rows) != 1:
        raise ManifestError(
            "required Results Registry compatibility obligation is missing or duplicated"
        )
    if len(compatibility) != 1:
        raise ManifestError(
            "compatibility array is reserved for the separate Results Registry binding"
        )
    for item in compatibility:
        contract_bytes = _check_reference(
            item["contract"], f"compatibility {item['id']}", source, bundle, tracked, source_commit
        )
        contract = _strict_json_object(contract_bytes, f"compatibility {item['id']} contract")
        if item["id"] == required_id:
            if (
                contract.get("contract_type") != obligation["contract_type"]
                or contract.get("component_id") != obligation["component_id"]
            ):
                raise ManifestError(
                    "required Results Registry compatibility contract has wrong type or component"
                )
            if item["evidence_required"]:
                raise ManifestError(
                    "Results Registry schema binding is a static validator obligation, "
                    "not a candidate-selected evidence gate"
                )
            _validate_results_registry_binding(
                item["contract"],
                contract_bytes,
                source,
                bundle,
                tracked,
                source_commit,
                candidate_artifacts=candidate_artifacts,
                components_by_id=components_by_id,
            )
        else:
            raise ManifestError(f"compatibility {item['id']} contract type is unsupported")


def _validate_compatibility_coverage(
    rows: list[dict[str, Any]],
    inventory_roles: list[dict[str, Any]],
    provider_gate_pairs: set[tuple[str, str]],
    components: list[dict[str, Any]],
    candidate: dict[str, Any],
    required_scopes: dict[str, list[dict[str, str | None]]],
    *,
    accepted: bool,
) -> None:
    """Require explicit compatibility inventory coverage for every shipped provider."""
    required_capabilities = {"python_distributions", "market_data_web_console"}
    expected = {
        provider
        for role in inventory_roles
        if role["capability"] in required_capabilities
        for provider in role["providers"]
    }
    actual_ids = [row["component_id"] for row in rows]
    actual = set(actual_ids)
    if len(actual_ids) != len(actual):
        raise ManifestError("duplicate compatibility provider coverage")
    if actual != expected:
        raise ManifestError(
            "compatibility coverage differs from source-pinned providers: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )

    components_by_id = {item["id"]: item for item in components}
    migrations = {item["id"]: item for item in candidate["migrations"]}
    rollbacks = {item["id"]: item for item in candidate["rollback"]}
    source_commit = candidate["source"]["commit"]
    for row in rows:
        component_id = row["component_id"]
        formats = row["formats"]
        if row["status"] == "unknown-blocking":
            if accepted:
                raise ManifestError(
                    f"accepted decision has unknown compatibility coverage: {component_id}"
                )
            continue
        if accepted and any(
            scope["runtime_status"] == "unknown-blocking" for scope in required_scopes[component_id]
        ):
            raise ManifestError(
                f"accepted decision has unresolved compatibility runtime scope: {component_id}"
            )
        format_ids = [item["format_id"] for item in formats]
        if len(format_ids) != len(set(format_ids)):
            raise ManifestError(f"duplicate compatibility format identity for {component_id}")
        if row["inventory_status"] == "none":
            continue
        if not formats:
            raise ManifestError(f"confirmed compatibility inventory is incomplete: {component_id}")
        for fmt in formats:
            if component_id not in fmt["producers"] and component_id not in fmt["consumers"]:
                raise ManifestError(
                    f"compatibility format does not involve its provider: {component_id}"
                )
            unknown_participants = (set(fmt["producers"]) | set(fmt["consumers"])) - set(
                components_by_id
            )
            if unknown_participants:
                raise ManifestError(
                    f"compatibility format references unknown components: {sorted(unknown_participants)}"
                )
            if accepted and not fmt["relations"]:
                raise ManifestError(
                    f"accepted decision has no predecessor relation for {component_id}/{fmt['format_id']}"
                )
            relation_keys: set[tuple[str, str, str, str, str]] = set()
            relation_scopes: dict[
                tuple[str, str, str, str, str], set[tuple[str, str | None, str]]
            ] = {}
            for relation in fmt["relations"]:
                predecessor, target = relation["predecessor"], relation["target"]
                scope = relation["scope"]
                scope_key = (scope["environment_id"], scope["runtime_id"], scope["runtime_status"])
                allowed_scopes = {
                    (item["environment_id"], item["runtime_id"], item["runtime_status"])
                    for item in required_scopes[component_id]
                }
                if scope_key not in allowed_scopes:
                    raise ManifestError(
                        "compatibility relation scope is outside the support profile"
                    )
                key = (
                    predecessor["release_id"],
                    predecessor["format_id"],
                    target["format_id"],
                    scope["environment_id"],
                    scope["runtime_id"] or "",
                )
                if key in relation_keys:
                    raise ManifestError("duplicate compatibility predecessor/target relation")
                relation_keys.add(key)
                relation_identity = (
                    predecessor["release_id"],
                    predecessor["candidate_id"],
                    predecessor["source_commit"],
                    predecessor["format_id"],
                    target["format_id"],
                )
                relation_scopes.setdefault(relation_identity, set()).add(scope_key)
                if (
                    predecessor["format_id"] != fmt["format_id"]
                    or target["format_id"] != fmt["format_id"]
                ):
                    raise ManifestError("compatibility relation format identity mismatch")
                target_component = components_by_id.get(target["component_id"])
                if (
                    target["source_commit"] != source_commit
                    or target["component_id"] != component_id
                    or target_component is None
                    or target["component_version"] != target_component["version"]
                    or target["descriptor_sha256"] != target_component["descriptor"]["sha256"]
                ):
                    raise ManifestError("compatibility relation target identity mismatch")
                disposition = relation["disposition"]
                if disposition not in {
                    "migrate",
                    "unchanged-compatible",
                    "reject",
                    "unknown-blocking",
                }:
                    raise ManifestError("compatibility relation disposition is unsupported")
                if disposition == "unknown-blocking":
                    if accepted:
                        raise ManifestError("accepted decision has unknown compatibility relation")
                    continue
                migration_id, rollback_id = relation["migration_id"], relation["rollback_id"]
                if disposition == "migrate":
                    migration, rollback = migrations.get(migration_id), rollbacks.get(rollback_id)
                    if (
                        migration is None
                        or rollback is None
                        or not migration["evidence_required"]
                        or not rollback["evidence_required"]
                        or migration["from"] != predecessor["release_id"]
                        or migration["to"] != target["component_version"]
                        or rollback["from_candidate"] != "self"
                        or rollback["to_candidate"] != predecessor["candidate_id"]
                    ):
                        raise ManifestError(
                            "migrating compatibility relation requires evidence-bound migration and rollback"
                        )
                elif migration_id is not None or rollback_id is not None:
                    raise ManifestError(
                        "non-migrating compatibility relation cannot reference migration or rollback"
                    )
            if accepted and fmt["relations"]:
                raise ManifestError(
                    "accepted compatibility format lacks resolved predecessor manifest and "
                    "source/bundle-bound target contract/artifact material for trusted verification"
                )
            expected_scopes = {
                (item["environment_id"], item["runtime_id"], item["runtime_status"])
                for item in required_scopes[component_id]
            }
            if any(scopes != expected_scopes for scopes in relation_scopes.values()):
                raise ManifestError(
                    "compatibility predecessor relation is missing a required environment/runtime scope"
                )


def compatibility_subject_sha256(subject: dict[str, Any]) -> str:
    """Hash a compatibility claim independently of candidate-bound evidence identity."""
    return sha256_bytes(canonical_json_bytes(subject))


def _compatibility_scope_keys(
    component_id: str,
    components_by_id: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    acceptance_rows: list[dict[str, Any]],
    compatibility_gates: dict[str, str],
) -> list[dict[str, str | None]]:
    """Expand exact provider acceptance environments across applicable runtimes."""
    environments = sorted(
        {
            row["environment"]
            for row in acceptance_rows
            if row["component_id"] == component_id
            and row["gate"] == compatibility_gates[component_id]
        }
    )
    if not environments:
        raise ManifestError(
            f"compatibility provider has no profile acceptance scope: {component_id}"
        )
    runtime_ids = (
        sorted(item["id"] for item in profile["python_runtimes"])
        if components_by_id[component_id]["kind"] == "python_distribution"
        else [None]
    )
    runtime_status = (
        "declared"
        if components_by_id[component_id]["kind"] == "python_distribution"
        else "unknown-blocking"
    )
    return [
        {
            "environment_id": environment,
            "runtime_id": runtime_id,
            "runtime_status": runtime_status,
        }
        for environment in environments
        for runtime_id in runtime_ids
    ]


def _compatibility_subjects(
    rows: list[dict[str, Any]],
    required_scopes: dict[str, list[dict[str, str | None]]],
) -> dict[str, tuple[str, dict[str, str | None]]]:
    """Return scoped owner-confirmation subjects keyed by digest."""
    required: dict[str, tuple[str, dict[str, str | None]]] = {}
    for coverage in rows:
        if coverage["status"] != "confirmed":
            continue
        component_id = coverage["component_id"]
        owner = coverage["owner"]
        if coverage["inventory_status"] == "none":
            for scope in required_scopes[component_id]:
                subject = {
                    "kind": "no_formats",
                    "component_id": component_id,
                    "owner": owner,
                    "scope": scope,
                }
                required[compatibility_subject_sha256(subject)] = (component_id, scope)
            continue
        for fmt in coverage["formats"]:
            for scope in required_scopes[component_id]:
                format_subject = {
                    "kind": "format_inventory",
                    "component_id": component_id,
                    "owner": owner,
                    "format_id": fmt["format_id"],
                    "format_kind": fmt["format_kind"],
                    "producers": sorted(fmt["producers"]),
                    "consumers": sorted(fmt["consumers"]),
                    "scope": scope,
                }
                required[compatibility_subject_sha256(format_subject)] = (component_id, scope)
            for relation in fmt["relations"]:
                if relation["disposition"] == "unknown-blocking":
                    continue
                relation_subject = {
                    "kind": "compatibility_relation",
                    "component_id": component_id,
                    "owner": owner,
                    "format_id": fmt["format_id"],
                    "relation": relation,
                }
                scope = relation["scope"]
                normalized_scope = {
                    "environment_id": scope["environment_id"],
                    "runtime_id": scope["runtime_id"],
                    "runtime_status": scope["runtime_status"],
                }
                relation_subject["scope"] = normalized_scope
                required[compatibility_subject_sha256(relation_subject)] = (
                    component_id,
                    normalized_scope,
                )
    return required


def _compatibility_provider_gate(component_id: str, policy: dict[str, Any]) -> str:
    suffix_length = policy["provider_evidence_gate_suffix_length"]
    suffix = hashlib.sha256(component_id.encode("utf-8")).hexdigest()[:suffix_length]
    return f"{policy['provider_evidence_gate_prefix']}{suffix}"


def _load_compatibility_coverage_policy(
    source: Path, bundle: Path, tracked: set[str], source_commit: str
) -> tuple[dict[str, Any], str]:
    raw = _read_path(
        "source",
        "schemas/platform-compatibility-coverage-policy-v1.json",
        "platform compatibility coverage policy",
        source,
        bundle,
        tracked,
        source_commit,
    )
    policy = _strict_json_object(raw, "platform compatibility coverage policy")
    if (
        set(policy)
        != {
            "contract_type",
            "contract_version",
            "required_capabilities",
            "provider_evidence_gate_prefix",
            "provider_evidence_gate_suffix_length",
        }
        or policy["contract_type"] != "platform_compatibility_coverage_policy"
        or policy["contract_version"] != 1
        or policy["required_capabilities"] != ["python_distributions", "market_data_web_console"]
        or policy["provider_evidence_gate_prefix"] != "compatibility:provider-"
        or policy["provider_evidence_gate_suffix_length"] != 16
    ):
        raise ManifestError("platform compatibility coverage policy is invalid")
    return policy, sha256_bytes(raw)


def _catalog_root(catalog_path: Any) -> PurePosixPath:
    if not isinstance(catalog_path, str):
        raise ManifestError("Results Registry catalog path is invalid")
    path = PurePosixPath(catalog_path)
    if path.is_absolute() or ".." in path.parts or path.name != "catalog.json":
        raise ManifestError("Results Registry catalog path is not canonical")
    return path.parent


def _path_within(raw_path: Any, root: PurePosixPath) -> bool:
    if not isinstance(raw_path, str):
        return False
    path = PurePosixPath(raw_path)
    return (
        not path.is_absolute()
        and all(part not in {".", ".."} for part in path.parts)
        and path.is_relative_to(root)
        and path != root
    )


def _resolve_registry_ref(
    reference: str,
    current_id: str,
    documents: dict[str, dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    path_to_id: dict[str, str],
) -> None:
    from urllib.parse import unquote, urlsplit

    parsed = urlsplit(reference)
    if parsed.query:
        raise ManifestError(
            f"query components are unsupported in Results Registry $ref: {reference}"
        )
    if parsed.scheme or parsed.netloc:
        if parsed.scheme == "urn" and parsed.scheme + ":" + parsed.path in by_id:
            target_id = parsed.scheme + ":" + parsed.path
        else:
            raise ManifestError(f"unsupported external Results Registry $ref: {reference}")
    elif not parsed.path:
        target_id = current_id
    else:
        current_path = PurePosixPath(by_id[current_id]["path"])
        normalized = PurePosixPath(current_path.parent, unquote(parsed.path))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ManifestError(f"dangling local Results Registry $ref: {reference}")
        target_id = path_to_id.get(normalized.as_posix(), "")
        if not target_id:
            raise ManifestError(f"dangling local Results Registry $ref: {reference}")
    target = documents[target_id]
    fragment = unquote(parsed.fragment)
    if not fragment:
        return
    if fragment.startswith("/"):
        current: Any = target
        for encoded in fragment[1:].split("/"):
            token = encoded.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
                current = current[int(token)]
            else:
                raise ManifestError(f"dangling JSON Pointer in Results Registry $ref: {reference}")
        return
    raise ManifestError(
        f"named anchor fragments are unsupported in Results Registry $ref: {reference}"
    )


def _check_reference(
    reference: Any,
    label: str,
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
) -> bytes:
    return _read_reference(
        reference, label, source_root, candidate_bundle_root, tracked, source_commit
    )


def _valid_npm_version(value: Any) -> bool:
    if not isinstance(value, str) or NPM_SEMVER_RE.fullmatch(value) is None:
        return False
    prerelease = value.split("+", 1)[0].partition("-")[2]
    return not any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease.split(".")
    )


def _normalized_package_identity(name: Any, version: Any, label: str) -> tuple[str, str]:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name
    ):
        raise ManifestError(f"{label} has an invalid distribution name")
    if not isinstance(version, str):
        raise ManifestError(f"{label} has an invalid distribution version")
    try:
        normalized_version = canonicalize_version(Version(version), strip_trailing_zero=True)
    except InvalidVersion as exc:
        raise ManifestError(f"{label} has an invalid distribution version") from exc
    return canonicalize_name(name), normalized_version


def _validate_core_metadata(
    content: bytes, label: str, minimum_metadata_version: tuple[int, int]
) -> tuple[str, str]:
    if not content or len(content) > MAX_DISTRIBUTION_METADATA_BYTES or b"\x00" in content:
        raise ManifestError(f"{label} exceeds or violates the metadata size limit")
    try:
        content.decode("utf-8")
        metadata = BytesParser(policy=policy.default).parsebytes(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"{label} is malformed core metadata") from exc
    if metadata.defects:
        raise ManifestError(f"{label} is malformed core metadata")
    names, versions, metadata_versions = (
        metadata.get_all("Name", []),
        metadata.get_all("Version", []),
        metadata.get_all("Metadata-Version", []),
    )
    if len(names) != 1 or len(versions) != 1 or len(metadata_versions) != 1:
        raise ManifestError(
            f"{label} must contain unique Name, Version, and Metadata-Version fields"
        )
    metadata_version = metadata_versions[0]
    if re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", metadata_version) is None:
        raise ManifestError(f"{label} has an invalid Metadata-Version")
    parsed_metadata_version = tuple(int(part) for part in metadata_version.split("."))
    if parsed_metadata_version[0] > 2:
        raise ManifestError(f"{label} uses unsupported Metadata-Version major")
    if parsed_metadata_version < minimum_metadata_version:
        minimum = ".".join(str(part) for part in minimum_metadata_version)
        raise ManifestError(
            f"{label} Metadata-Version {metadata_version} is below required {minimum}"
        )
    return _normalized_package_identity(names[0], versions[0], label)


def _archive_member_parts(name: str, *, directory: bool, label: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ManifestError(f"{label} contains an unsafe archive path")
    path = name[:-1] if directory and name.endswith("/") else name
    parts = tuple(path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"{label} contains an unsafe archive path")
    return parts


def _bounded_zip_entry_count(content: bytes, label: str, *, max_entries: int | None = None) -> int:
    """Count central-directory records before ZipFile can materialize them."""
    entry_limit = MAX_DISTRIBUTION_ARCHIVE_ENTRIES if max_entries is None else max_entries
    tail_start = max(0, len(content) - (22 + 65_535))
    eocd_offset = -1
    eocd_fields: tuple[int, int, int, int, int, int] | None = None
    search_end = len(content)
    while True:
        relative = content.rfind(ZIP_EOCD_SIGNATURE, tail_start, search_end)
        if relative < 0:
            break
        if relative + 22 <= len(content):
            _signature, disk, cd_disk, disk_entries, total_entries, cd_size, cd_offset, comment = (
                struct.unpack_from("<4s4H2LH", content, relative)
            )
            if relative + 22 + comment == len(content):
                eocd_offset = relative
                eocd_fields = (disk, cd_disk, disk_entries, total_entries, cd_size, cd_offset)
                break
        search_end = relative
    if eocd_offset < 0 or eocd_fields is None:
        raise ManifestError(f"{label} lacks a valid ZIP end-of-central-directory record")
    disk, cd_disk, disk_entries, total_entries, cd_size, cd_offset = eocd_fields
    if 0xFFFF in {disk_entries, total_entries} or 0xFFFFFFFF in {cd_size, cd_offset}:
        raise ManifestError(f"{label} uses unsupported ZIP64 layout")
    if disk or cd_disk or disk_entries != total_entries:
        raise ManifestError(f"{label} uses unsupported multi-disk ZIP layout")
    if (
        eocd_offset >= 20
        and content[eocd_offset - 20 : eocd_offset - 16] == ZIP64_LOCATOR_SIGNATURE
    ):
        raise ManifestError(f"{label} uses unsupported ZIP64 layout")
    if cd_offset + cd_size != eocd_offset:
        raise ManifestError(f"{label} has inconsistent ZIP central-directory bounds")

    cursor = cd_offset
    count = 0
    while cursor < eocd_offset:
        if (
            cursor + 46 > eocd_offset
            or content[cursor : cursor + 4] != ZIP_CENTRAL_DIRECTORY_SIGNATURE
        ):
            raise ManifestError(f"{label} has a malformed ZIP central directory")
        name_length, extra_length, comment_length = struct.unpack_from("<3H", content, cursor + 28)
        compressed_size, expanded_size = struct.unpack_from("<2L", content, cursor + 20)
        disk_start = struct.unpack_from("<H", content, cursor + 34)[0]
        local_offset = struct.unpack_from("<L", content, cursor + 42)[0]
        record_end = cursor + 46 + name_length + extra_length + comment_length
        if record_end > eocd_offset:
            raise ManifestError(f"{label} has a truncated ZIP central-directory record")
        if disk_start != 0:
            raise ManifestError(f"{label} uses unsupported multi-disk ZIP layout")
        if 0xFFFFFFFF in {
            compressed_size,
            expanded_size,
            local_offset,
        }:
            raise ManifestError(f"{label} uses unsupported ZIP64 layout")
        extra_cursor = cursor + 46 + name_length
        extra_end = extra_cursor + extra_length
        while extra_cursor < extra_end:
            if extra_cursor + 4 > extra_end:
                raise ManifestError(f"{label} has malformed ZIP extra fields")
            extra_id, extra_size = struct.unpack_from("<2H", content, extra_cursor)
            extra_cursor += 4
            if extra_cursor + extra_size > extra_end:
                raise ManifestError(f"{label} has malformed ZIP extra fields")
            if extra_id == 0x0001:
                raise ManifestError(f"{label} uses unsupported ZIP64 layout")
            extra_cursor += extra_size
        count += 1
        if count > entry_limit:
            raise ManifestError(f"{label} exceeds the archive entry-count limit")
        cursor = record_end
    if cursor != eocd_offset or count != total_entries:
        raise ManifestError(f"{label} has inconsistent ZIP central-directory entry count")
    return count


def _read_wheel_metadata(
    content: bytes,
    label: str,
    *,
    validate_portable_policy: bool = False,
    runtime_versions: tuple[str, ...] = (),
    filename_tags: frozenset[Any] | None = None,
    source_python_specifier: SpecifierSet | None = None,
) -> tuple[str, str, str]:
    try:
        _bounded_zip_entry_count(content, label)
        archive = zipfile.ZipFile(io.BytesIO(content))
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_DISTRIBUTION_ARCHIVE_ENTRIES:
                raise ManifestError(f"{label} exceeds the archive entry-count limit")
            seen: set[str] = set()
            metadata_paths: list[tuple[str, str]] = []
            wheel_paths: list[tuple[str, str]] = []
            expanded = 0
            for info in infos:
                is_dir = info.is_dir()
                parts = _archive_member_parts(info.filename, directory=is_dir, label=label)
                if info.filename in seen:
                    raise ManifestError(f"{label} contains duplicate archive paths")
                seen.add(info.filename)
                mode = info.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (is_dir and file_type not in {0, stat.S_IFDIR}) or (
                    not is_dir and file_type not in {0, stat.S_IFREG}
                ):
                    raise ManifestError(f"{label} contains an unsupported archive member kind")
                if info.file_size < 0 or info.compress_size < 0:
                    raise ManifestError(f"{label} contains invalid archive sizes")
                if not is_dir:
                    expanded += info.file_size
                    if expanded > MAX_DISTRIBUTION_EXPANDED_BYTES:
                        raise ManifestError(f"{label} exceeds the expanded-size limit")
                if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA":
                    if is_dir:
                        raise ManifestError(f"{label} has invalid wheel metadata member")
                    if info.file_size > MAX_DISTRIBUTION_METADATA_BYTES:
                        raise ManifestError(f"{label} exceeds the metadata size limit")
                    metadata_paths.append((info.filename, parts[0][: -len(".dist-info")]))
                if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "WHEEL":
                    if is_dir:
                        raise ManifestError(f"{label} has invalid WHEEL metadata member")
                    if info.file_size > MAX_DISTRIBUTION_METADATA_BYTES:
                        raise ManifestError(f"{label} exceeds the metadata size limit")
                    wheel_paths.append((info.filename, parts[0][: -len(".dist-info")]))
            if len(metadata_paths) != 1:
                raise ManifestError(f"{label} must contain exactly one .dist-info/METADATA")
            metadata_path, dist_info_identity = metadata_paths[0]
            if validate_portable_policy and (
                len(wheel_paths) != 1 or wheel_paths[0][1] != dist_info_identity
            ):
                raise ManifestError(
                    f"{label} must contain exactly one corresponding .dist-info/WHEEL"
                )
            # Decompress each member under a cumulative bound so forged size fields
            # cannot hide an oversized payload behind a small central directory.
            actual_expanded = 0
            metadata_content: bytes | None = None
            wheel_content: bytes | None = None
            for info in infos:
                if info.is_dir():
                    continue
                with archive.open(info) as member:
                    chunks: list[bytes] = []
                    member_size = 0
                    while chunk := member.read(1024 * 1024):
                        member_size += len(chunk)
                        actual_expanded += len(chunk)
                        if actual_expanded > MAX_DISTRIBUTION_EXPANDED_BYTES:
                            raise ManifestError(f"{label} exceeds the expanded-size limit")
                        if info.filename == metadata_path or (
                            validate_portable_policy
                            and wheel_paths
                            and info.filename == wheel_paths[0][0]
                        ):
                            if member_size > MAX_DISTRIBUTION_METADATA_BYTES:
                                raise ManifestError(f"{label} exceeds the metadata size limit")
                            chunks.append(chunk)
                    if info.filename == metadata_path:
                        metadata_content = b"".join(chunks)
                    if (
                        validate_portable_policy
                        and wheel_paths
                        and info.filename == wheel_paths[0][0]
                    ):
                        if member_size > MAX_DISTRIBUTION_METADATA_BYTES:
                            raise ManifestError(f"{label} exceeds the metadata size limit")
                        wheel_content = b"".join(chunks)
                    if member_size != info.file_size:
                        raise ManifestError(f"{label} archive member size is inconsistent")
            if metadata_content is None:
                raise ManifestError(f"{label} wheel metadata is missing")
            if validate_portable_policy:
                if wheel_content is None:
                    raise ManifestError(f"{label} wheel metadata is missing")
                wheel_tags = _validate_portable_wheel_metadata(wheel_content, label)
                if filename_tags is None or wheel_tags != filename_tags:
                    raise ManifestError(f"{label} WHEEL tags differ from filename tags")
                _validate_wheel_requires_python(
                    metadata_content, label, runtime_versions, source_python_specifier
                )
            return (
                *_validate_core_metadata(metadata_content, f"{label} METADATA", (1, 1)),
                dist_info_identity,
            )
    except ManifestError:
        raise
    except (
        OSError,
        EOFError,
        RuntimeError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise ManifestError(f"{label} is not a valid bounded wheel archive") from exc


def _validate_portable_wheel_metadata(content: bytes, label: str) -> set[Any]:
    try:
        content.decode("utf-8")
        message = BytesParser(policy=policy.default).parsebytes(content)
    except (UnicodeError, ValueError) as exc:
        raise ManifestError(f"{label} has malformed WHEEL metadata") from exc
    if message.defects:
        raise ManifestError(f"{label} has malformed WHEEL metadata")
    versions = message.get_all("Wheel-Version", [])
    roots = message.get_all("Root-Is-Purelib", [])
    tags = message.get_all("Tag", [])
    if len(versions) != 1 or versions[0] != "1.0":
        raise ManifestError(f"{label} has unsupported or duplicate Wheel-Version")
    if len(roots) != 1 or roots[0].strip().lower() != "true":
        raise ManifestError(f"{label} must have exactly one Root-Is-Purelib: true")
    if not tags or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ManifestError(f"{label} has invalid or missing WHEEL Tag headers")
    try:
        expanded: set[Any] = set()
        for tag in tags:
            expanded.update(parse_tag(tag.strip()))
    except (ValueError, TypeError) as exc:
        raise ManifestError(f"{label} has invalid WHEEL Tag header") from exc
    allowed = {
        parse_tag("py3-none-any"),
        parse_tag("py2-none-any").union(parse_tag("py3-none-any")),
    }
    if expanded not in allowed:
        raise ManifestError(f"{label} has unsupported WHEEL tags")
    return expanded


def _validate_wheel_requires_python(
    content: bytes,
    label: str,
    runtime_versions: tuple[str, ...],
    source_specifier: SpecifierSet | None,
) -> None:
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except (UnicodeError, ValueError) as exc:
        raise ManifestError(f"{label} has malformed wheel METADATA") from exc
    requires = message.get_all("Requires-Python", [])
    if len(requires) > 1:
        raise ManifestError(f"{label} has duplicate Requires-Python headers")
    if len(requires) != 1 or not requires[0].strip():
        raise ManifestError(f"{label} must have exactly one nonempty Requires-Python header")
    try:
        specifier = SpecifierSet(requires[0])
        if not str(specifier):
            raise InvalidSpecifier
    except InvalidSpecifier as exc:
        raise ManifestError(f"{label} has invalid Requires-Python") from exc
    if source_specifier is None or specifier != source_specifier:
        raise ManifestError(f"{label} Requires-Python differs from source package metadata")
    if not runtime_versions or any(not specifier.contains(version) for version in runtime_versions):
        raise ManifestError(f"{label} Requires-Python is incompatible with supported runtimes")


class _ExpansionLimitedReader:
    def __init__(self, source: Any, limit: int, label: str) -> None:
        self.source = source
        self.limit = limit
        self.label = label
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        request_size = remaining + 1 if size < 0 else min(size, remaining + 1)
        data = self.source.read(request_size)
        self.total += len(data)
        if self.total > self.limit:
            raise ManifestError(f"{self.label} exceeds the expanded-size limit")
        return data

    def close(self) -> None:
        self.source.close()


def _read_sdist_metadata(
    content: bytes, label: str
) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
    try:
        gzip_stream = gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb")
        with (
            gzip_stream,
            tarfile.open(
                fileobj=_ExpansionLimitedReader(
                    gzip_stream, MAX_DISTRIBUTION_EXPANDED_BYTES, label
                ),
                mode="r|",
            ) as archive,
        ):
            root: str | None = None
            seen: set[str] = set()
            pkg_info: bytes | None = None
            pyproject: bytes | None = None
            expanded = 0
            count = 0
            for member in archive:
                count += 1
                if count > MAX_DISTRIBUTION_ARCHIVE_ENTRIES:
                    raise ManifestError(f"{label} exceeds the archive entry-count limit")
                is_dir = member.isdir()
                if not is_dir and not member.isfile():
                    raise ManifestError(f"{label} contains an unsupported archive member kind")
                parts = _archive_member_parts(member.name, directory=is_dir, label=label)
                if member.name in seen:
                    raise ManifestError(f"{label} contains duplicate archive paths")
                seen.add(member.name)
                if len(parts) == 1 and is_dir:
                    if root is None:
                        root = parts[0]
                    elif root != parts[0]:
                        raise ManifestError(f"{label} contains multiple top-level roots")
                    continue
                if len(parts) < 2:
                    raise ManifestError(f"{label} must have one top-level source root")
                if root is None:
                    root = parts[0]
                if parts[0] != root:
                    raise ManifestError(f"{label} contains multiple top-level roots")
                if member.size < 0:
                    raise ManifestError(f"{label} contains an invalid archive member size")
                if not is_dir:
                    expanded += member.size
                    if expanded > MAX_DISTRIBUTION_EXPANDED_BYTES:
                        raise ManifestError(f"{label} exceeds the expanded-size limit")
                is_pkg_info = parts == (root, "PKG-INFO")
                is_pyproject = parts == (root, "pyproject.toml")
                if is_pkg_info or is_pyproject:
                    if is_dir or member.size > MAX_DISTRIBUTION_METADATA_BYTES:
                        raise ManifestError(f"{label} exceeds the metadata size limit")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ManifestError(f"{label} metadata member cannot be read")
                    metadata_content = extracted.read(MAX_DISTRIBUTION_METADATA_BYTES + 1)
                    if len(metadata_content) != member.size:
                        raise ManifestError(f"{label} metadata member size is inconsistent")
                    if is_pkg_info:
                        pkg_info = metadata_content
                    else:
                        pyproject = metadata_content
            if root is None or pkg_info is None or pyproject is None:
                raise ManifestError(f"{label} must contain root PKG-INFO and pyproject.toml")
            root_name, root_version = parse_sdist_filename(f"{root}.tar.gz")
            pkg_info_identity = _validate_core_metadata(pkg_info, f"{label} PKG-INFO", (2, 2))
            pyproject_data = tomllib.loads(pyproject.decode("utf-8"))
            project = pyproject_data.get("project")
            if not isinstance(project, dict):
                raise ManifestError(f"{label} pyproject.toml lacks project metadata")
            dynamic = project.get("dynamic", [])
            allowed_dynamic_fields = {
                "version",
                "description",
                "readme",
                "requires-python",
                "license",
                "license-files",
                "authors",
                "maintainers",
                "keywords",
                "classifiers",
                "urls",
                "scripts",
                "gui-scripts",
                "entry-points",
                "dependencies",
                "optional-dependencies",
            }
            if (
                not isinstance(dynamic, list)
                or any(not isinstance(field, str) for field in dynamic)
                or len(dynamic) != len(set(dynamic))
                or any(field not in allowed_dynamic_fields for field in dynamic)
            ):
                raise ManifestError(f"{label} pyproject.toml has malformed project.dynamic")
            has_static_version = "version" in project
            declares_dynamic_version = "version" in dynamic
            if has_static_version and declares_dynamic_version:
                raise ManifestError(
                    f"{label} pyproject.toml declares version both statically and dynamically"
                )
            if not has_static_version and not declares_dynamic_version:
                raise ManifestError(
                    f"{label} pyproject.toml must declare version statically or dynamically"
                )
            pyproject_version = (
                project.get("version") if has_static_version else pkg_info_identity[1]
            )
            pyproject_identity = _normalized_package_identity(
                project.get("name"), pyproject_version, f"{label} pyproject.toml"
            )
            return (
                pkg_info_identity,
                _normalized_package_identity(root_name, str(root_version), f"{label} source root"),
                pyproject_identity,
            )
    except ManifestError:
        raise
    except (OSError, EOFError, UnicodeError, ValueError, zlib.error, tarfile.TarError) as exc:
        raise ManifestError(f"{label} is not a valid bounded modern sdist archive") from exc


def _validate_python_distribution_artifact(
    path: str,
    content: bytes,
    component: dict[str, Any],
    *,
    validate_portable_policy: bool = False,
    runtime_versions: tuple[str, ...] = (),
    source_python_specifier: SpecifierSet | None = None,
) -> str:
    label = f"Python distribution artifact for {component['id']}"
    expected = _normalized_package_identity(component["id"], component["version"], label)
    filename = PurePosixPath(path).name
    if filename.endswith(".whl"):
        try:
            wheel_name, wheel_version, _build, filename_tags = parse_wheel_filename(filename)
        except InvalidWheelFilename as exc:
            raise ManifestError(f"{label} has an invalid wheel filename") from exc
        filename_identity = (
            canonicalize_name(str(wheel_name)),
            canonicalize_version(wheel_version, strip_trailing_zero=True),
        )
        if validate_portable_policy:
            allowed_tags = {
                parse_tag("py3-none-any"),
                parse_tag("py2-none-any").union(parse_tag("py3-none-any")),
            }
            if filename_tags not in allowed_tags:
                raise ManifestError(f"{label} has unsupported wheel filename tags")
        metadata_name, metadata_version, dist_info_identity = _read_wheel_metadata(
            content,
            label,
            validate_portable_policy=validate_portable_policy,
            runtime_versions=runtime_versions,
            filename_tags=filename_tags if validate_portable_policy else None,
            source_python_specifier=source_python_specifier,
        )
        metadata_identity = (metadata_name, metadata_version)
        try:
            dist_info_name, dist_info_version, _build, _tags = parse_wheel_filename(
                f"{dist_info_identity}-py3-none-any.whl"
            )
        except InvalidWheelFilename as exc:
            raise ManifestError(f"{label} has an invalid .dist-info identity") from exc
        dist_info_normalized = (
            canonicalize_name(str(dist_info_name)),
            canonicalize_version(dist_info_version, strip_trailing_zero=True),
        )
        if filename_identity != expected or metadata_identity != expected:
            raise ManifestError(f"{label} wheel identity differs from source component metadata")
        if dist_info_normalized != expected:
            raise ManifestError(
                f"{label} .dist-info identity differs from source component metadata"
            )
        return "wheel"
    if filename.endswith(".tar.gz"):
        try:
            filename_name, filename_version = parse_sdist_filename(filename)
        except InvalidSdistFilename as exc:
            raise ManifestError(f"{label} has an invalid sdist filename") from exc
        filename_identity = (
            canonicalize_name(str(filename_name)),
            canonicalize_version(filename_version, strip_trailing_zero=True),
        )
        pkg_info_identity, root_identity, pyproject_identity = _read_sdist_metadata(content, label)
        if any(
            identity != expected
            for identity in (
                filename_identity,
                pkg_info_identity,
                root_identity,
                pyproject_identity,
            )
        ):
            raise ManifestError(f"{label} sdist identity differs from source component metadata")
        return "sdist"
    raise ManifestError(f"{label} has an unsupported Python distribution format")


def _validate_resolver_version(lock: dict[str, Any], label: str) -> None:
    version = lock.get("version")
    if lock.get("ecosystem") == "node":
        valid = _valid_npm_version(version)
    else:
        try:
            Version(version)
        except (InvalidVersion, TypeError):
            valid = False
        else:
            valid = isinstance(version, str)
    if not valid:
        raise ManifestError(f"{label} resolver version must be one concrete version")


def _requirements_logical_lines(content: bytes, label: str) -> list[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{label} is not UTF-8") from exc
    logical_lines: list[str] = []
    pending = ""
    for physical_line in text.splitlines():
        line = physical_line.rstrip()
        if not pending and (not line.strip() or line.lstrip().startswith("#")):
            continue
        trailing_slashes = len(line) - len(line.rstrip("\\"))
        if trailing_slashes % 2:
            pending += line[:-1]
            continue
        combined = (pending + line).strip()
        pending = ""
        combined = re.split(r"\s+#", combined, maxsplit=1)[0].strip()
        if combined:
            logical_lines.append(combined)
    if pending:
        raise ManifestError(f"{label} ends with an unterminated line continuation")
    if not logical_lines:
        raise ManifestError(f"{label} must contain at least one pinned requirement")
    return logical_lines


def _validate_hashed_requirements_v1(content: bytes, label: str) -> tuple[tuple[str, str], ...]:
    """Validate the deliberately narrow, fully hashed requirements dialect.

    This is not a general pip requirements parser. Includes, installer options,
    editables, URLs, local paths, ranges, and environment expansion are rejected.
    """
    seen: set[str] = set()
    pinned: list[tuple[str, str]] = []
    for line in _requirements_logical_lines(content, label):
        hashes, requirement_text = _extract_requirement_hashes(line)
        if "${" in requirement_text:
            raise ManifestError(f"{label} does not allow environment variable expansion")
        if not hashes or "--hash" in requirement_text:
            raise ManifestError(f"{label} every requirement must have SHA-256 hashes")
        if len(hashes) != len(set(value.lower() for value in hashes)):
            raise ManifestError(f"{label} contains a duplicate SHA-256 hash")
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise ManifestError(f"{label} contains an unsupported requirement: {exc}") from exc
        if requirement.url is not None:
            raise ManifestError(f"{label} does not allow direct URL requirements")
        if requirement.marker is not None:
            raise ManifestError(f"{label} does not allow requirement markers")
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise ManifestError(f"{label} requirements must use an exact non-wildcard == pin")
        try:
            pinned_version = canonicalize_version(
                Version(specifiers[0].version), strip_trailing_zero=True
            )
        except InvalidVersion as exc:
            raise ManifestError(f"{label} contains an invalid pinned version") from exc
        normalized_name = canonicalize_name(requirement.name)
        if normalized_name in seen:
            raise ManifestError(f"{label} contains duplicate normalized package names")
        seen.add(normalized_name)
        pinned.append((normalized_name, pinned_version))
    return tuple(sorted(pinned))


def _extract_requirement_hashes(line: str) -> tuple[list[str], str]:
    """Parse the narrow grammar: requirement/marker followed only by hashes."""
    tokens: list[tuple[int, int, str]] = []
    quote: str | None = None
    escaped = False
    token_start: int | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            if token_start is None:
                token_start = index
            quote = char
            index += 1
            continue
        if char.isspace():
            if token_start is not None:
                tokens.append((token_start, index, line[token_start:index]))
                token_start = None
            index += 1
            continue
        if token_start is None:
            token_start = index
        index += 1
    if token_start is not None:
        tokens.append((token_start, len(line), line[token_start:]))

    option_index = next(
        (index for index, (_start, _end, token) in enumerate(tokens) if token.startswith("--hash")),
        None,
    )
    if option_index is None:
        return [], line.strip()

    prefix = line[: tokens[option_index][0]].strip()
    if not prefix:
        raise ManifestError("requirements hash options must follow a requirement and marker")
    hashes: list[str] = []
    for _start, _end, token in tokens[option_index:]:
        match = re.fullmatch(r"--hash=sha256:([a-fA-F0-9]{64})", token)
        if match is None:
            raise ManifestError(
                "requirements contain a malformed, unsupported, or misplaced hash option"
            )
        hashes.append(match.group(1))
    return hashes, prefix


def _strict_lock_json(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestError) as exc:
        raise ManifestError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{label} root must be an object")
    return value


def _valid_npm_package_path(value: str) -> bool:
    parts = value.split("/")
    if not parts or parts[0] != "node_modules" or any(part in {"", ".", ".."} for part in parts):
        return False
    index = 0
    while index < len(parts):
        if parts[index] != "node_modules":
            return False
        index += 1
        if index >= len(parts):
            return False
        if parts[index].startswith("@"):
            index += 1
            if index >= len(parts):
                return False
        index += 1
    return True


def _validate_sri(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must include an SRI integrity value")
    tokens = value.split()
    if not tokens:
        raise ManifestError(f"{label} must include an SRI integrity value")
    for token in tokens:
        match = SRI_TOKEN_RE.fullmatch(token)
        if match is None:
            raise ManifestError(f"{label} contains malformed SRI integrity data")
        algorithm, encoded = match.groups()
        try:
            digest = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ManifestError(f"{label} contains malformed SRI base64 data") from exc
        if len(digest) != hashlib.new(algorithm).digest_size:
            raise ManifestError(f"{label} contains an SRI digest of the wrong length")


def _validate_npm_lock_v3(content: bytes, descriptor: bytes, label: str) -> None:
    lock = _strict_lock_json(content, label)
    package = _strict_lock_json(descriptor, f"{label} source package.json")
    if type(lock.get("lockfileVersion")) is not int or lock["lockfileVersion"] != 3:
        raise ManifestError(f"{label} must use npm package-lock format version 3")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise ManifestError(f"{label} must contain a packages object with a root package")
    root = packages[""]
    expected_name, expected_version = package.get("name"), package.get("version")
    if not isinstance(expected_name, str) or not _valid_npm_version(expected_version):
        raise ManifestError(f"{label} source package metadata is incomplete")
    if lock.get("name") != expected_name or lock.get("version") != expected_version:
        raise ManifestError(f"{label} top-level package identity differs from package.json")
    if root.get("name") != expected_name or root.get("version") != expected_version:
        raise ManifestError(f"{label} root package identity differs from package.json")
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        expected = package.get(field, {})
        actual = root.get(field, {})
        if not isinstance(expected, dict) or not isinstance(actual, dict) or actual != expected:
            raise ManifestError(f"{label} root {field} differ from package.json")
    for path, record in packages.items():
        if path == "":
            continue
        if not isinstance(path, str) or not _valid_npm_package_path(path):
            raise ManifestError(f"{label} contains an unsupported package path")
        if not isinstance(record, dict) or record.get("link"):
            raise ManifestError(f"{label} contains an unsupported link or package record")
        if not _valid_npm_version(record.get("version")):
            raise ManifestError(f"{label} package {path} lacks a concrete semantic version")
        resolved = record.get("resolved")
        if not isinstance(resolved, str):
            raise ManifestError(f"{label} package {path} lacks a resolved HTTPS artifact")
        url = urlsplit(resolved)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ManifestError(f"{label} package {path} has an unsupported resolved artifact URL")
        _validate_sri(record.get("integrity"), f"{label} package {path}")


def _validate_lock_content(
    lock: dict[str, Any],
    content: bytes,
    components_by_id: dict[str, dict[str, Any]],
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
) -> tuple[tuple[str, str], ...] | None:
    label = f"lock {lock['id']}"
    ecosystem, lock_format, resolver = lock["ecosystem"], lock["format"], lock["resolver"]
    if (ecosystem, lock_format, resolver) == ("python", "requirements-hashed-v1", "uv"):
        _validate_resolver_version(lock, label)
        pinned = _validate_hashed_requirements_v1(content, label)
        candidate_names = {
            canonicalize_name(component_id) for component_id in lock["covered_components"]
        }
        for name, pin in pinned:
            if name not in candidate_names:
                continue
            component = next(
                item for item in lock["covered_components"] if canonicalize_name(item) == name
            )
            try:
                component_version = canonicalize_version(
                    Version(components_by_id[component]["version"]), strip_trailing_zero=True
                )
            except InvalidVersion as exc:
                raise ManifestError(f"selected component version is invalid: {component}") from exc
            if pin != component_version:
                raise ManifestError(
                    f"{label} candidate distribution pin differs from selected component: {component}"
                )
        return pinned
    if (ecosystem, lock_format, resolver) == ("node", "npm-lock-v3", "npm"):
        _validate_resolver_version(lock, label)
        covered_components = lock["covered_components"]
        if len(covered_components) != 1:
            raise ManifestError(f"{label} must cover exactly one npm project root")
        component = components_by_id[covered_components[0]]
        if component["kind"] != "web_console":
            raise ManifestError(f"{label} root component must be a Web Console")
        descriptor = _read_reference(
            component["descriptor"],
            f"{label} source package.json",
            source_root,
            candidate_bundle_root,
            tracked,
            source_commit,
        )
        _validate_npm_lock_v3(content, descriptor, label)
        return None
    raise ManifestError(
        f"{label} has no validator for adapter {ecosystem}/{lock_format}/{resolver}"
    )


def _marker_variables(node: Any) -> set[str]:
    if isinstance(node, Variable):
        return {node.value}
    if isinstance(node, (list, tuple)):
        return set().union(*(_marker_variables(item) for item in node))
    return set()


def _requirement_pin_policy(
    requirement_texts: list[str], label: str, python_runtimes: list[dict[str, Any]]
) -> dict[str, dict[str, list[SpecifierSet]]]:
    """Parse source requirements per exact Python runtime for modeled version markers."""
    policy: dict[str, dict[str, list[SpecifierSet]]] = {
        runtime["id"]: {} for runtime in python_runtimes
    }
    modeled_marker_variables = {"python_version", "python_full_version"}
    for requirement_text in requirement_texts:
        try:
            requirement = Requirement(requirement_text)
        except (InvalidRequirement, TypeError) as exc:
            raise ManifestError(
                f"{label} contains an invalid requirement: {requirement_text}"
            ) from exc
        if requirement.url is not None:
            raise ManifestError(f"{label} does not support direct URL requirements")
        if requirement.extras:
            raise ManifestError(f"{label} does not support dependency-requested extras")
        marker = requirement.marker
        if marker is not None:
            unsupported = _marker_variables(marker._markers) - modeled_marker_variables
            if unsupported:
                raise ManifestError(
                    f"{label} references unmodeled environment marker variables: "
                    f"{sorted(unsupported)}"
                )
        for runtime in python_runtimes:
            version = Version(runtime["version"])
            environment = {
                "python_version": f"{version.release[0]}.{version.release[1]}",
                "python_full_version": str(version),
            }
            if marker is not None and not marker.evaluate(environment=environment):
                continue
            policy[runtime["id"]].setdefault(canonicalize_name(requirement.name), []).append(
                requirement.specifier
            )
    return policy


def _source_python_requirement_policies(
    source_root: Path,
    candidate_bundle_root: Path,
    components: list[dict[str, Any]],
    tracked: set[str],
    source_commit: str,
    python_runtimes: list[dict[str, Any]],
    python_dependency_selection: dict[str, Any] | None = None,
) -> tuple[
    dict[str, dict[str, dict[str, list[SpecifierSet]]]],
    dict[str, dict[str, dict[str, list[SpecifierSet]]]],
    dict[str, list[str]],
    list[dict[str, Any]],
    str | None,
]:
    """Derive base-runtime and build requirements from exact source descriptor blobs."""
    runtime: dict[str, dict[str, list[SpecifierSet]]] = {}
    build: dict[str, dict[str, list[SpecifierSet]]] = {}
    selected_requirements: dict[str, list[str]] = {}
    descriptor_refs: list[dict[str, Any]] = []
    python_components = sorted(
        (item for item in components if item["kind"] == "python_distribution"),
        key=lambda item: item["id"],
    )
    rows_by_component: dict[str, list[str]] = {}
    selection_digest: str | None = None
    if python_dependency_selection is not None:
        rows = python_dependency_selection.get("providers")
        canonical_selection = _canonical_v3_python_dependency_selection(
            [component["id"] for component in python_components]
        )
        expected_ids = [row["component_id"] for row in canonical_selection["providers"]]
        if (
            isinstance(python_dependency_selection.get("schema_version"), bool)
            or python_dependency_selection.get("schema_version") != 1
            or not isinstance(rows, list)
            or [row.get("component_id") for row in rows if isinstance(row, dict)] != expected_ids
        ):
            raise ManifestError(
                "Python dependency selection must contain one canonical row for every provider"
            )
        rows_by_component = {row["component_id"]: row["extras"] for row in rows}
        if any(
            not isinstance(extras, list) or extras != sorted(set(extras))
            for extras in rows_by_component.values()
        ):
            raise ManifestError("Python dependency selection extras must be sorted and unique")
        for component_id, extras in rows_by_component.items():
            if component_id not in _V3_SELECTED_PYTHON_EXTRAS and extras:
                raise ManifestError(
                    f"Python dependency selection includes unsupported extras for {component_id}"
                )
        canonical_extras = {
            row["component_id"]: row["extras"] for row in canonical_selection["providers"]
        }
        for component_id, expected_extras in canonical_extras.items():
            if rows_by_component[component_id] != expected_extras:
                raise ManifestError(
                    f"Python dependency selection differs from the selected profile for {component_id}"
                )
        selection_digest = sha256_bytes(
            json.dumps(
                python_dependency_selection,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
    for component in components:
        if component["kind"] != "python_distribution":
            continue
        descriptor = component["descriptor"]
        descriptor_refs.append({"component_id": component["id"], "descriptor": descriptor})
        raw = _read_reference(
            descriptor,
            f"{component['id']} source descriptor",
            source_root,
            candidate_bundle_root,
            tracked,
            source_commit,
        )
        try:
            project_data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(f"{component['id']} source descriptor is invalid TOML") from exc
        project = project_data.get("project")
        if not isinstance(project, dict):
            raise ManifestError(f"{component['id']} source descriptor lacks a project table")
        dynamic = project.get("dynamic", [])
        if (
            not isinstance(dynamic, list)
            or any(not isinstance(item, str) for item in dynamic)
            or len(dynamic) != len(set(dynamic))
        ):
            raise ManifestError(f"{component['id']} project.dynamic is malformed")
        if "dependencies" in dynamic and "dependencies" in project:
            raise ManifestError(
                f"{component['id']} project.dependencies cannot be both static and dynamic"
            )
        if "dependencies" in dynamic:
            raise ManifestError(f"{component['id']} dynamic project.dependencies are unsupported")
        selected_extras = rows_by_component.get(component["id"], [])
        if selected_extras and "optional-dependencies" in dynamic:
            raise ManifestError(
                f"{component['id']} selected optional-dependencies cannot be dynamic"
            )
        base_dependencies = project.get("dependencies", [])
        if not isinstance(base_dependencies, list) or any(
            not isinstance(item, str) for item in base_dependencies
        ):
            raise ManifestError(f"{component['id']} project.dependencies must be a string array")
        effective_dependencies = list(base_dependencies)
        if python_dependency_selection is not None and selected_extras:
            optional_dependencies = project.get("optional-dependencies", {})
            if not isinstance(optional_dependencies, dict):
                raise ManifestError(
                    f"{component['id']} project.optional-dependencies must be a table"
                )
            for extra in selected_extras:
                extra_dependencies = optional_dependencies.get(extra)
                if not isinstance(extra_dependencies, list) or any(
                    not isinstance(item, str) for item in extra_dependencies
                ):
                    raise ManifestError(
                        f"{component['id']} selected optional dependency group is missing or malformed: {extra}"
                    )
                effective_dependencies.extend(extra_dependencies)
        selected_requirements[component["id"]] = effective_dependencies

        build_system = project_data.get("build-system")
        if not isinstance(build_system, dict):
            raise ManifestError(f"{component['id']} build-system metadata is missing or invalid")
        build_requirements = build_system.get("requires")
        backend = build_system.get("build-backend")
        backend_path = build_system.get("backend-path")
        if (
            not isinstance(build_requirements, list)
            or any(not isinstance(item, str) for item in build_requirements)
            or not isinstance(backend, str)
            or not backend.strip()
            or (
                backend_path is not None
                and (
                    not isinstance(backend_path, list)
                    or any(not isinstance(item, str) for item in backend_path)
                )
            )
        ):
            raise ManifestError(f"{component['id']} build-system metadata is malformed")

        runtime[component["id"]] = _requirement_pin_policy(
            effective_dependencies,
            (
                f"{component['id']} effective runtime requirements"
                if python_dependency_selection is not None
                else f"{component['id']} project.dependencies"
            ),
            python_runtimes,
        )
        build[component["id"]] = _requirement_pin_policy(
            build_requirements, f"{component['id']} build-system.requires", python_runtimes
        )
    descriptor_refs.sort(key=lambda row: row["component_id"])
    return runtime, build, selected_requirements, descriptor_refs, selection_digest


def _runtime_resolver_input_bytes(
    requirements_by_component: dict[str, list[str]],
    descriptor_refs: list[dict[str, Any]],
    selection: dict[str, Any],
    runtime: dict[str, Any],
) -> bytes:
    version = Version(runtime["version"])
    environment = {
        "python_version": f"{version.release[0]}.{version.release[1]}",
        "python_full_version": str(version),
    }
    requirements: list[dict[str, str]] = []
    for component_id, requirement_texts in sorted(requirements_by_component.items()):
        for requirement_text in requirement_texts:
            requirement = Requirement(requirement_text)
            if requirement.marker is not None and not requirement.marker.evaluate(
                environment=environment
            ):
                continue
            requirements.append({"component_id": component_id, "requirement": requirement_text})
    requirements.sort(key=lambda row: (row["component_id"], row["requirement"]))
    input_document = {
        "contract_type": "python_effective_resolver_input_v1",
        "selection_sha256": sha256_bytes(
            json.dumps(
                selection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ),
        "source_descriptors": descriptor_refs,
        "python_runtime_id": runtime["id"],
        "observed_runtime": {"implementation": "cpython", "version": runtime["version"]},
        "requirements": requirements,
    }
    return json.dumps(
        input_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _validate_runtime_receipt_reference(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"origin", "path", "sha256"}:
        raise ManifestError(f"{label} must contain exactly origin, path, and sha256")
    if any(not isinstance(value[field], str) for field in ("origin", "path", "sha256")):
        raise ManifestError(f"{label} fields must be strings")
    if value["origin"] not in {"source", "bundle"}:
        raise ManifestError(f"{label} has an invalid origin")
    if not value["path"] or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ManifestError(f"{label} has an invalid path or SHA-256 digest")
    return value


def _validate_python_runtime_resolution_receipt(
    lock: dict[str, Any],
    runtime: dict[str, Any],
    selection: dict[str, Any],
    selection_digest: str,
    requirements_by_component: dict[str, list[str]],
    descriptor_refs: list[dict[str, Any]],
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    reference_collector: Any,
) -> None:
    reference = lock["python_runtime_resolution_receipt"]
    if reference["origin"] != "bundle":
        raise ManifestError("Python runtime resolution receipt must originate in bundle")
    raw = _read_reference(
        reference, f"Python runtime resolution {lock['id']}", source, bundle, tracked, source_commit
    )
    receipt = _strict_json_object(raw, f"Python runtime resolution {lock['id']} receipt")
    expected_fields = {
        "schema_version",
        "contract_type",
        "source_commit",
        "selection_sha256",
        "source_descriptors",
        "resolver_input",
        "python_runtime_id",
        "observed_runtime",
        "target_environment",
        "resolver",
        "resolver_version",
        "resolver_index_policy",
        "lock_sha256",
        "outcome",
        "run_id",
        "attempt",
    }
    if set(receipt) != expected_fields:
        raise ManifestError("Python runtime resolution receipt has unexpected fields")
    if isinstance(receipt["schema_version"], bool) or receipt["schema_version"] != 1:
        raise ManifestError("Python runtime resolution receipt schema version is invalid")
    expected_descriptors = [
        {"component_id": row["component_id"], "descriptor": row["descriptor"]}
        for row in descriptor_refs
    ]
    if receipt["source_descriptors"] != expected_descriptors:
        raise ManifestError("Python runtime resolution receipt descriptor binding mismatch")
    if receipt["selection_sha256"] != selection_digest:
        raise ManifestError("Python runtime resolution receipt selection digest mismatch")
    resolver_input = _runtime_resolver_input_bytes(
        requirements_by_component, descriptor_refs, selection, runtime
    )
    input_reference = _validate_runtime_receipt_reference(
        receipt["resolver_input"], "Python runtime effective resolver input reference"
    )
    if input_reference["origin"] != "bundle":
        raise ManifestError("Python runtime effective resolver input must originate in bundle")
    recorded_input = _check_reference(
        input_reference,
        f"Python runtime resolution {lock['id']} effective resolver input",
        source,
        bundle,
        tracked,
        source_commit,
    )
    if recorded_input != resolver_input:
        raise ManifestError("Python runtime resolution effective input bytes mismatch")
    policy_ref = _validate_runtime_receipt_reference(
        receipt["resolver_index_policy"], "Python runtime resolver/index policy reference"
    )
    _check_reference(
        policy_ref,
        f"Python runtime resolution {lock['id']} resolver/index policy",
        source,
        bundle,
        tracked,
        source_commit,
    )
    expected_identity = {
        "contract_type": "python_runtime_resolution_v1",
        "source_commit": source_commit,
        "python_runtime_id": runtime["id"],
        "observed_runtime": {"implementation": "cpython", "version": runtime["version"]},
        "target_environment": lock["target_environment"],
        "resolver": lock["resolver"],
        "resolver_version": lock["version"],
        "lock_sha256": lock["file"]["sha256"],
        "outcome": "resolved",
    }
    if any(receipt.get(key) != value for key, value in expected_identity.items()):
        raise ManifestError("Python runtime resolution receipt identity mismatch")
    if (
        not isinstance(receipt["run_id"], str)
        or not receipt["run_id"].strip()
        or isinstance(receipt["attempt"], bool)
        or not isinstance(receipt["attempt"], int)
        or receipt["attempt"] < 1
    ):
        raise ManifestError("Python runtime resolution receipt run identity is invalid")
    reference_collector.capture(f"runtime_resolution:{lock['id']}:receipt", reference)
    reference_collector.capture(f"runtime_resolution:{lock['id']}:input", input_reference)
    reference_collector.capture(f"runtime_resolution:{lock['id']}:policy", policy_ref)


def _validate_source_requirement_coverage(
    pins: tuple[tuple[str, str], ...],
    covered_components: list[str],
    requirements_by_component: dict[str, dict[str, list[SpecifierSet]]],
    components_by_normalized_name: dict[str, dict[str, Any]],
    label: str,
    *,
    purpose: str,
    runtime_lock_coverage: set[tuple[str, str, str, str, str | None]],
    environment: str,
    runtime_id: str | None,
) -> None:
    pin_map = dict(pins)
    needed: dict[str, list[SpecifierSet]] = {}
    for component_id in covered_components:
        for normalized_name, specifiers in requirements_by_component[component_id].items():
            needed.setdefault(normalized_name, []).extend(specifiers)

    for name, component in components_by_normalized_name.items():
        try:
            component_version = canonicalize_version(
                Version(component["version"]), strip_trailing_zero=True
            )
        except InvalidVersion as exc:
            raise ManifestError(f"invalid internal component version: {component['id']}") from exc
        if name in pin_map and pin_map[name] != component_version:
            raise ManifestError(
                f"{label} internal candidate pin differs from selected component: {component['id']}"
            )

    for name, specifiers in needed.items():
        component = components_by_normalized_name.get(name)
        if component is not None:
            if purpose in {"build", "bootstrap"}:
                raise ManifestError(
                    f"{label} cannot consume internal build requirement before candidate artifact binding: {component['id']}"
                )
            try:
                version = Version(component["version"])
            except InvalidVersion as exc:
                raise ManifestError(
                    f"invalid internal component version: {component['id']}"
                ) from exc
            if any(not specifier.contains(version) for specifier in specifiers):
                raise ManifestError(
                    f"{label} internal component version violates source requirements: {component['id']}"
                )
            if (
                runtime_id is None
                or (component["id"], "python", "runtime", environment, runtime_id)
                not in runtime_lock_coverage
            ):
                raise ManifestError(
                    f"{label} internal dependency lacks applicable runtime coverage: {component['id']}"
                )
            continue
        if name not in pin_map:
            raise ManifestError(f"{label} omits source-declared dependency pin: {name}")
        try:
            pinned_version = Version(pin_map[name])
        except InvalidVersion as exc:
            raise ManifestError(f"{label} has an invalid pin for {name}") from exc
        if any(not specifier.contains(pinned_version) for specifier in specifiers):
            raise ManifestError(f"{label} pin for {name} violates source-declared constraints")


def _check_identity(obj: dict[str, Any], field: str, label: str) -> str:
    expected = object_identity(obj, field)
    actual = obj.get(field)
    if not isinstance(actual, str) or not IDENTITY_RE.fullmatch(actual) or actual != expected:
        raise ManifestError(f"{label} does not match its canonical content hash")
    return expected


def _clean_exact_source(root: Path, commit: str) -> None:
    head = _git(root, "rev-parse", "HEAD")
    if not GIT_SHA_RE.fullmatch(commit) or commit != head:
        raise ManifestError("candidate source commit must equal repository HEAD")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ManifestError("source tree must be clean, including untracked files")


def _validate_v3_web_console(
    candidate: dict[str, Any],
    profile: dict[str, Any],
    components: list[dict[str, Any]],
    locks: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    collector: Any,
) -> Any:
    """Verify immutable web source/lock/artifact facts and retain pending owner gates."""
    from scripts.validated_release_facts import WebArtifactMemberFact, WebConsoleFact

    declaration = candidate["web_console"]
    component_id = declaration["component_id"]
    component = next(
        (row for row in components if row["id"] == component_id and row["kind"] == "web_console"),
        None,
    )
    if component is None or declaration["component_id"] != component["id"]:
        raise ManifestError("v3 web-console binding does not identify the candidate console")
    expected_paths = sorted(
        path
        for path in tracked
        if path.startswith("web/market-data-console/")
        and not set(PurePosixPath(path).parts)
        & {"node_modules", "dist", "coverage", "test-results"}
    )
    inputs = declaration["source_inputs"]
    if [row["path"] for row in inputs] != expected_paths:
        raise ManifestError("v3 console input manifest must exactly cover sorted tracked inputs")
    input_by_path: dict[str, dict[str, Any]] = {}
    input_facts = []
    for row in inputs:
        if row["origin"] != "source":
            raise ManifestError("v3 console source inputs must originate in source")
        _check_reference(row, "v3 console source input", source, bundle, tracked, source_commit)
        input_by_path[row["path"]] = row
        input_facts.append(collector.capture(f"web_console:input:{row['path']}", row))

    package_path = "web/market-data-console/package.json"
    lock_path = "web/market-data-console/package-lock.json"
    if package_path not in input_by_path or lock_path not in input_by_path:
        raise ManifestError("v3 console inputs must bind package.json and package-lock.json")
    if component["descriptor"] != input_by_path[package_path]:
        raise ManifestError("v3 package input differs from candidate web-console descriptor")
    lock = next((row for row in locks if row["id"] == declaration["npm_lock_id"]), None)
    if (
        lock is None
        or lock["ecosystem"] != "node"
        or lock["covered_components"] != [component_id]
        or lock["format"] != "npm-lock-v3"
        or lock["resolver"] != "npm"
    ):
        raise ManifestError("v3 web-console binding does not select its validated npm lock")
    lock_bytes = _check_reference(
        lock["file"], "v3 npm lock", source, bundle, tracked, source_commit
    )
    source_lock_bytes = _check_reference(
        input_by_path[lock_path], "v3 source npm lock", source, bundle, tracked, source_commit
    )
    if lock_bytes != source_lock_bytes:
        raise ManifestError("v3 candidate npm lock differs from source package-lock.json")
    lock_fact = collector.capture(f"web_console:npm_lock:{lock['id']}", lock["file"])

    artifact = next(
        (
            row
            for row in artifacts
            if row["component"] == component_id and row["kind"] == "web_assets"
        ),
        None,
    )
    if artifact is None or artifact["file"]["sha256"] != declaration["artifact_sha256"]:
        raise ManifestError(
            "v3 web-console artifact digest is not bound to its web_assets artifact"
        )
    artifact_bytes = _read_reference(
        artifact["file"],
        "v3 web-console ZIP artifact",
        source,
        bundle,
        tracked,
        source_commit,
        MAX_WEB_ASSET_ARCHIVE_BYTES,
    )
    if len(artifact_bytes) > MAX_WEB_ASSET_ARCHIVE_BYTES:
        raise ManifestError("v3 web-console ZIP exceeds the archive size limit")
    members = declaration["artifact_members"]
    member_paths = [row["path"] for row in members]
    if member_paths != sorted(member_paths) or len(member_paths) != len(set(member_paths)):
        raise ManifestError("v3 web-console ZIP members must be sorted and unique")
    if sha256_bytes(canonical_json_bytes(members)) != declaration["member_manifest_sha256"]:
        raise ManifestError("v3 web-console member manifest digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(artifact_bytes)) as archive:
            actual_members = []
            infos = archive.infolist()
            if len(infos) > MAX_WEB_ASSET_ARCHIVE_ENTRIES:
                raise ManifestError("v3 web-console ZIP exceeds the member count limit")
            if sum(info.file_size for info in infos) > MAX_WEB_ASSET_EXPANDED_BYTES:
                raise ManifestError("v3 web-console ZIP exceeds the expanded size limit")
            seen_folded: set[str] = set()
            file_paths_folded: set[str] = set()
            for info in infos:
                name = info.filename
                posix = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0o170000
                folded = name.casefold()
                if (
                    posix.is_absolute()
                    or str(posix) != name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or "\\" in name
                    or "\x00" in name
                    or info.is_dir()
                    or mode not in {0, 0o100000}
                    or info.date_time != (1980, 1, 1, 0, 0, 0)
                    or info.flag_bits & 0x1
                    or folded in seen_folded
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or info.extra
                    or info.comment
                    or archive.comment
                ):
                    raise ManifestError("v3 web-console ZIP has unsafe/nondeterministic members")
                seen_folded.add(folded)
                file_paths_folded.add(folded)
            if any(
                "/".join(PurePosixPath(path).parts[:index]).casefold() in file_paths_folded
                for path in file_paths_folded
                for index in range(1, len(PurePosixPath(path).parts))
            ):
                raise ManifestError("v3 web-console ZIP has a file/directory path conflict")
            total_streamed = 0
            for info in infos:
                member_digest = hashlib.sha256()
                member_size = 0
                with archive.open(info, "r") as member:
                    while chunk := member.read(64 * 1024):
                        member_size += len(chunk)
                        total_streamed += len(chunk)
                        if (
                            member_size > info.file_size
                            or total_streamed > MAX_WEB_ASSET_EXPANDED_BYTES
                        ):
                            raise ManifestError("v3 web-console ZIP member exceeded declared size")
                        member_digest.update(chunk)
                if member_size != info.file_size:
                    raise ManifestError("v3 web-console ZIP member size differs from its header")
                actual_members.append(
                    {
                        "path": info.filename,
                        "size": member_size,
                        "sha256": member_digest.hexdigest(),
                    }
                )
    except (
        OSError,
        RuntimeError,
        EOFError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ) as exc:
        raise ManifestError("v3 web-console artifact is not a valid ZIP") from exc
    if actual_members != members:
        raise ManifestError("v3 web-console ZIP members differ from the declared manifest")
    artifact_fact = collector.capture(
        f"web_console:artifact:{artifact['file']['path']}", artifact["file"]
    )

    web_profile = profile["web_console"]
    unresolved_bindings: list[str] = []
    for group_name, group in web_profile.items():
        for binding_name, binding in group.items():
            if binding["status"] == "pending":
                unresolved_bindings.append(f"{group_name}.{binding_name}")
                continue
            value = binding["value"]
            binding_path = f"{group_name}.{binding_name}"
            if binding_name in {"node_version", "npm_version"}:
                if not isinstance(value, str) or not NPM_SEMVER_RE.fullmatch(value):
                    raise ManifestError(f"v3 {binding_path} must be an exact semantic version")
            elif binding_name == "architecture":
                if value not in {"x86_64", "aarch64"}:
                    raise ManifestError("v3 Linux build architecture is unsupported")
            elif binding_name == "context":
                expected_os = "linux" if group_name == "linux_deployment" else "darwin"
                expected_keys = (
                    {"os", "environment_id", "runtime_id"}
                    if group_name == "linux_deployment"
                    else {"os", "environment_id"}
                )
                if (
                    not isinstance(value, dict)
                    or set(value) != expected_keys
                    or value.get("os") != expected_os
                    or not isinstance(value.get("environment_id"), str)
                    or not value["environment_id"]
                    or (
                        group_name == "linux_deployment"
                        and (
                            not isinstance(value.get("runtime_id"), str) or not value["runtime_id"]
                        )
                    )
                ):
                    raise ManifestError(f"v3 {binding_path} has an invalid OS/environment context")
            elif binding_name == "runtime":
                if (
                    not isinstance(value, dict)
                    or set(value) != {"environment_id", "runtime_id", "mode"}
                    or not isinstance(value.get("environment_id"), str)
                    or not value["environment_id"]
                    or not isinstance(value.get("runtime_id"), str)
                    or not value["runtime_id"]
                    or value.get("mode") not in {"static_asset", "container", "managed_service"}
                ):
                    raise ManifestError("v3 Linux serving runtime binding is not typed")
                if value["environment_id"] not in profile["target_environments"]:
                    raise ManifestError(
                        "v3 Linux serving runtime must use the sole target environment"
                    )
            elif binding_name == "artifact_use":
                if not isinstance(value, dict) or value.get("mode") not in {
                    "same_artifact",
                    "separate_rebuild",
                }:
                    raise ManifestError("v3 Darwin artifact use must select a typed reuse mode")
            elif binding_name in {
                "provisioning_material",
                "registry_policy",
                "install_script_policy",
                "image",
                "recipe",
                "configuration",
                "reconstruction_receipt",
                "evidence",
                "browser_contexts",
            }:
                if not isinstance(value, (dict, list)) or not value:
                    raise ManifestError(f"v3 {binding_path} must be a nonempty structured binding")
            else:
                raise ManifestError(f"v3 {binding_path} is not a recognized binding")
            # Bound values are structurally checked, but this validator cannot
            # authenticate their owner provenance or acceptance evidence yet.
            unresolved_bindings.append(f"{binding_path} (bound, unverified)")
            if group_name == "linux_deployment" and binding_name == "context":
                if value.get("environment_id") not in profile["target_environments"]:
                    raise ManifestError("v3 Linux deployment must use the sole target environment")
            if group_name == "darwin_acceptance" and binding_name == "context":
                environment_id = value["environment_id"]
                if environment_id in profile["target_environments"]:
                    raise ManifestError("v3 Darwin acceptance cannot become a deployment target")
                console_acceptance_environments = {
                    row["environment"]
                    for row in profile["acceptance_requirements"]
                    if row["component_id"] == component_id
                }
                if environment_id not in console_acceptance_environments:
                    raise ManifestError(
                        "v3 Darwin context must match a declared Console acceptance environment"
                    )
            if group_name == "darwin_acceptance" and binding_name == "artifact_use":
                if not isinstance(value, dict):
                    raise ManifestError("v3 Darwin artifact use binding must be an object")
                if (
                    value.get("mode") == "same_artifact"
                    and value.get("artifact_sha256") != artifact["file"]["sha256"]
                ):
                    raise ManifestError("v3 Darwin acceptance substituted the Linux artifact")
                if (
                    value.get("mode") == "separate_rebuild"
                    and value.get("source_commit") != source_commit
                ):
                    raise ManifestError("v3 Darwin rebuild must use the same source commit")
    linux_context = web_profile["linux_deployment"]["context"]
    serving_runtime = web_profile["linux_serving"]["runtime"]
    if linux_context["status"] == "bound" and serving_runtime["status"] == "bound":
        if serving_runtime["value"]["runtime_id"] != linux_context["value"]["runtime_id"]:
            raise ManifestError(
                "v3 Linux deployment context references a different serving runtime"
            )
    return WebConsoleFact(
        component_id=component_id,
        npm_lock_id=lock["id"],
        source_inputs=tuple(input_facts),
        npm_lock_reference=lock_fact,
        artifact_reference=artifact_fact,
        artifact_members=tuple(
            WebArtifactMemberFact(row["path"], row["size"], row["sha256"]) for row in members
        ),
        member_manifest_sha256=declaration["member_manifest_sha256"],
        pending_bindings=tuple(sorted(unresolved_bindings)),
    )


def _canonical_repository_uri(value: str) -> None:
    if value != CANONICAL_REPOSITORY:
        raise ManifestError("source.repository must equal the canonical repository URI")


def _discover_distributions(
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
    *,
    allow_dynamic_requires_python: bool = False,
) -> dict[str, tuple[str, str, bytes, str | None, list[str]]]:
    found: dict[str, tuple[str, str, bytes, str | None, list[str]]] = {}
    candidates = [source_root / "pyproject.toml"]
    for product_root in ("applications", "modules", "plugins", "research"):
        directory = source_root / product_root
        if directory.is_dir():
            candidates.extend(directory.rglob("pyproject.toml"))
    for path in sorted(candidates):
        if not path.is_file():
            continue
        try:
            relative_path = path.relative_to(source_root).as_posix()
            content = _read_path(
                "source",
                relative_path,
                "Python package metadata",
                source_root,
                candidate_bundle_root,
                tracked,
                source_commit,
            )
            metadata = tomllib.loads(content.decode("utf-8")).get("project", {})
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ManifestError(
                f"cannot read package metadata {path.relative_to(source_root)}: {exc}"
            ) from exc
        name, version = metadata.get("name"), metadata.get("version")
        if isinstance(name, str) and isinstance(version, str):
            requires_python = metadata.get("requires-python")
            dynamic = metadata.get("dynamic", [])
            dynamic_requires_python = (
                allow_dynamic_requires_python
                and isinstance(dynamic, list)
                and "requires-python" in dynamic
            )
            if dynamic_requires_python:
                requires_python = None
            elif not isinstance(requires_python, str) or not requires_python:
                raise ManifestError(f"Python package metadata lacks requires-python: {name}")
            dependencies = metadata.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                not isinstance(dependency, str) for dependency in dependencies
            ):
                raise ManifestError(f"Python package dependencies are invalid: {name}")
            if name in found:
                raise ManifestError(f"duplicate Python distribution metadata: {name}")
            found[name] = (version, relative_path, content, requires_python, dependencies)
    if len(found) != 26:
        raise ManifestError(f"expected 26 Python distributions, discovered {len(found)}")
    return found


def _check_component_inventory(
    source_root: Path,
    candidate_bundle_root: Path,
    components: list[dict[str, Any]],
    tracked: set[str],
    source_commit: str,
    python_runtimes: list[dict[str, Any]],
) -> dict[str, SpecifierSet]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in components:
        component_id = item["id"]
        existing: dict[str, Any] | None = by_id.get(component_id)
        if existing is not None:
            raise ManifestError(f"duplicate component id: {component_id}")
        by_id[component_id] = item

    expected = _discover_distributions(source_root, candidate_bundle_root, tracked, source_commit)
    python_specifiers: dict[str, SpecifierSet] = {}
    selected_python: dict[str, dict[str, Any]] = {}
    for component in components:
        if component.get("kind") != "python_distribution":
            continue
        normalized_name = canonicalize_name(component["id"])
        if normalized_name in selected_python:
            raise ManifestError(f"duplicate normalized Python component name: {component['id']}")
        selected_python[normalized_name] = component
    console_path = "web/market-data-console/package.json"
    console_content = _read_path(
        "source",
        console_path,
        "market data console metadata",
        source_root,
        candidate_bundle_root,
        tracked,
        source_commit,
    )
    console_metadata = json.loads(console_content.decode("utf-8"))
    expected[console_metadata["name"]] = (
        console_metadata["version"],
        console_path,
        console_content,
        None,
        [],
    )
    for package, (
        version,
        descriptor,
        descriptor_content,
        requires_python,
        dependencies,
    ) in expected.items():
        component_item = by_id.get(package)
        if component_item is None:
            raise ManifestError(f"component inventory omits package: {package}")
        descriptor_ref = component_item["descriptor"]
        if (
            component_item.get("version") != version
            or descriptor_ref["path"] != descriptor
            or descriptor_ref["origin"] != "source"
            or descriptor_ref["sha256"] != sha256_bytes(descriptor_content)
        ):
            raise ManifestError(f"component metadata differs from package: {package}")
        if component_item.get("kind") != (
            "web_console" if package.startswith("@") else "python_distribution"
        ):
            raise ManifestError(f"component kind differs from package: {package}")
        if requires_python is not None:
            try:
                specifier = SpecifierSet(requires_python)
            except InvalidSpecifier as exc:
                raise ManifestError(f"invalid requires-python for package: {package}") from exc
            if component_item.get("kind") == "python_distribution":
                python_specifiers[canonicalize_name(package)] = specifier
            for runtime in python_runtimes:
                try:
                    runtime_version = Version(runtime["version"])
                except InvalidVersion as exc:
                    raise ManifestError("Python runtime version is invalid") from exc
                if not specifier.contains(runtime_version, prereleases=False):
                    raise ManifestError(
                        f"Python runtime {runtime['version']} does not satisfy {package} requires-python {requires_python}"
                    )
        for dependency_text in dependencies:
            try:
                requirement = Requirement(dependency_text)
            except InvalidRequirement as exc:
                raise ManifestError(
                    f"invalid Python dependency for package {package}: {dependency_text}"
                ) from exc
            dependency = selected_python.get(canonicalize_name(requirement.name))
            if dependency is None:
                continue
            if requirement.url is not None:
                raise ManifestError(
                    f"internal Python dependency uses a direct URL: {package} -> {requirement.name}"
                )
            if requirement.extras:
                raise ManifestError(
                    f"internal Python dependency requests extras: {package} -> {requirement.name}"
                )
            if requirement.marker is not None:
                raise ManifestError(
                    f"internal Python dependency marker is unsupported: {package} -> {requirement.name}"
                )
            try:
                dependency_version = Version(dependency["version"])
            except InvalidVersion as exc:
                raise ManifestError(
                    f"selected Python component version is invalid: {dependency['id']}"
                ) from exc
            if not requirement.specifier.contains(dependency_version):
                raise ManifestError(
                    f"selected Python component version does not satisfy dependency: "
                    f"{package} requires {dependency_text}, selected {dependency['id']} "
                    f"{dependency['version']}"
                )
        for index, source_ref in enumerate(component_item["source_files"]):
            if source_ref["origin"] != "source":
                raise ManifestError(f"{package} source_files must originate in source")
            _check_reference(
                source_ref,
                f"{package} source_files[{index}]",
                source_root,
                candidate_bundle_root,
                tracked,
                source_commit,
            )
    extra = set(by_id) - set(expected)
    for component_id in sorted(extra):
        extra_component = by_id[component_id]
        if extra_component["kind"] not in {"operator_ui", "recovery_ui", "service", "runbook"}:
            raise ManifestError(f"component inventory has unknown package: {component_id}")
        _check_reference(
            extra_component["descriptor"],
            f"{component_id} descriptor",
            source_root,
            candidate_bundle_root,
            tracked,
            source_commit,
        )
        for source_ref in extra_component["source_files"]:
            _check_reference(
                source_ref,
                f"{component_id} source_files",
                source_root,
                candidate_bundle_root,
                tracked,
                source_commit,
            )
    return python_specifiers


def _load_component_inventory(
    reference: dict[str, Any],
    source_root: Path,
    candidate_bundle_root: Path,
    tracked: set[str],
    source_commit: str,
    policy: _ManifestPolicy = _V2_POLICY,
) -> tuple[list[dict[str, Any]], str]:
    if reference.get("origin") != "source":
        raise ManifestError("component_inventory must be pinned to source")
    raw = _read_reference(
        reference,
        "component inventory",
        source_root,
        candidate_bundle_root,
        tracked,
        source_commit,
    )
    try:
        inventory = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ManifestError) as exc:
        raise ManifestError(f"cannot load component inventory: {exc}") from exc
    if not isinstance(inventory, dict):
        raise ManifestError("component inventory root must be an object")
    try:
        jsonschema = importlib.import_module("jsonschema")
        schema_bytes = _read_path(
            "source",
            policy.schema_path.as_posix(),
            "manifest schema",
            source_root,
            candidate_bundle_root,
            tracked,
            source_commit,
        )
        schema = json.loads(schema_bytes.decode("utf-8"))
        inventory_schema = {
            "$schema": schema["$schema"],
            "$ref": "#/$defs/componentInventory",
            "$defs": schema["$defs"],
        }
        validator = jsonschema.Draft202012Validator(
            inventory_schema, format_checker=jsonschema.FormatChecker()
        )
        errors = list(validator.iter_errors(inventory))
    except (ImportError, KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot validate component inventory: {exc}") from exc
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "component_inventory"
        raise ManifestError(f"{location}: {error.message}")
    roles = inventory.get("roles")
    if not isinstance(roles, list) or any(not isinstance(role, dict) for role in roles):
        raise ManifestError("component inventory roles must be a list of objects")
    return roles, sha256_bytes(raw)


def _validate_inventory_relationships(
    roles: list[dict[str, Any]],
    components: list[dict[str, Any]],
    capabilities: dict[str, Any],
    known_gates: set[str],
) -> tuple[set[tuple[str, str]], set[str]]:
    component_ids = {item["id"] for item in components}
    role_by_id: dict[str, dict[str, Any]] = {}
    capability_names: list[str] = []
    gate_ids: set[str] = set()
    for role in roles:
        role_id = role["id"]
        if role_id in role_by_id:
            raise ManifestError(f"duplicate component inventory role: {role_id}")
        role_by_id[role_id] = role
        capability_name = role["capability"]
        if capability_name not in capabilities:
            raise ManifestError(
                f"inventory role {role_id} references unknown capability: {capability_name}"
            )
        if role_id != capability_name:
            raise ManifestError(f"inventory role id must equal its capability identity: {role_id}")
        capability_names.append(capability_name)
        unknown = set(role["providers"]) - component_ids
        if unknown:
            raise ManifestError(
                f"inventory role {role_id} references unknown providers: {sorted(unknown)}"
            )
        for gate in role["required_gates"]:
            if not GATE_ID_RE.fullmatch(gate):
                raise ManifestError(f"inventory role {role_id} has invalid gate identity: {gate}")
            if gate in gate_ids:
                raise ManifestError(f"duplicate component inventory gate identity: {gate}")
            gate_ids.add(gate)
        unknown_gates = set(role["required_gates"]) - known_gates
        if unknown_gates:
            raise ManifestError(
                f"inventory role {role_id} references unknown gates: {sorted(unknown_gates)}"
            )
        if role["required"] and not role["providers"] and not role["blocked_by"]:
            raise ManifestError(
                f"required inventory role {role_id} has no provider or blocked_by dependency"
            )

    if set(capability_names) != set(capabilities) or len(capability_names) != len(capabilities):
        missing = sorted(set(capabilities) - set(capability_names))
        extra = sorted(set(capability_names) - set(capabilities))
        raise ManifestError(
            f"component inventory must cover each capability exactly once: "
            f"missing={missing}, extra={extra}"
        )

    unresolved_roles: set[str] = set()
    providers_by_capability: dict[str, set[str]] = {name: set() for name in capabilities}
    gates_by_capability: dict[str, set[str]] = {name: set() for name in capabilities}
    provider_gate_pairs: set[tuple[str, str]] = set()
    unresolved_by_capability: dict[str, set[str]] = {name: set() for name in capabilities}
    for role in roles:
        role_id = role["id"]
        capability_name = role["capability"]
        for dependency in role["blocked_by"]:
            if dependency["type"] != "github_issue" or not DEPENDENCY_ISSUE_RE.fullmatch(
                dependency["url"]
            ):
                raise ManifestError(
                    f"inventory role {role_id} has invalid blocked_by dependency reference"
                )
        if role["required"] and role["blocked_by"]:
            unresolved_roles.add(role_id)
            unresolved_by_capability[capability_name].add(role_id)
        providers_by_capability[capability_name].update(role["providers"])
        gates_by_capability[capability_name].update(role["required_gates"])
        provider_gate_pairs.update(
            (gate, provider)
            for gate in role["required_gates"]
            if not gate.startswith(("lock:", "rebuild:"))
            for provider in role["providers"]
        )

    inventoried_components = set().union(*providers_by_capability.values())
    if inventoried_components != component_ids:
        raise ManifestError(
            "candidate component inventory differs from source-pinned providers: "
            f"missing={sorted(inventoried_components - component_ids)}, "
            f"extra={sorted(component_ids - inventoried_components)}"
        )
    for name, capability in capabilities.items():
        expected_components = providers_by_capability[name]
        if set(capability["components"]) != expected_components:
            raise ManifestError(f"capability {name} providers differ from source-pinned inventory")
        if set(capability["required_gates"]) != gates_by_capability[name]:
            raise ManifestError(f"capability {name} gates differ from source-pinned inventory")
        unresolved = unresolved_by_capability[name]
        if unresolved and capability["status"] != "blocked":
            raise ManifestError(
                f"capability {name} must be blocked for unresolved required roles: "
                f"{sorted(unresolved)}"
            )
        if capability["status"] == "blocked" and not unresolved:
            raise ManifestError(
                f"capability {name} is blocked without an unresolved required inventory role"
            )
    return provider_gate_pairs, unresolved_roles


def _normalized_distributions(rows: Any, label: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(rows, list):
        raise ManifestError(f"{label} resolved_distributions must be a list")
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "version"}:
            raise ManifestError(f"{label} has an invalid resolved distribution")
        name, version = row["name"], row["version"]
        if not isinstance(name, str) or not isinstance(version, str):
            raise ManifestError(f"{label} distribution name and version must be strings")
        canonical_name = canonicalize_name(name)
        try:
            canonical_version = canonicalize_version(Version(version), strip_trailing_zero=True)
        except InvalidVersion as exc:
            raise ManifestError(f"{label} has an invalid distribution version: {version}") from exc
        if not canonical_name or canonical_name in seen:
            raise ManifestError(f"{label} has duplicate normalized distribution names")
        seen.add(canonical_name)
        normalized.append((canonical_name, canonical_version))
    return tuple(sorted(normalized))


def _validate_lock_reconstructions(
    payload: dict[str, Any],
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    candidate_id: str,
    profile_id: str,
    python_locks: list[dict[str, Any]],
    python_runtimes: list[dict[str, Any]],
    normalized_lock_pins: dict[str, tuple[tuple[str, str], ...]],
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], set[str]]:
    locks = {item["id"]: item for item in python_locks}
    runtimes = {item["id"]: item for item in python_runtimes}
    expected = {
        (lock["id"], lock["python_runtime_id"], lock["target_environment"]) for lock in python_locks
    }
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    receipt_hashes: set[str] = set()
    receipt_keys = {
        "schema_version",
        "contract_type",
        "candidate_id",
        "profile_id",
        "source_commit",
        "lock_id",
        "lock_sha256",
        "environment",
        "python_runtime_id",
        "observed_runtime",
        "run_id",
        "attempt",
        "outcome",
        "resolved_distributions",
    }
    for row in payload["lock_reconstructions"]:
        key = (row["lock_id"], row["python_runtime_id"], row["environment"])
        if key in rows:
            raise ManifestError("duplicate Python lock reconstruction tuple")
        if key not in expected:
            raise ManifestError("Python lock reconstruction does not match a declared lock/runtime")
        lock, runtime = locks[key[0]], runtimes[key[1]]
        ref = row["receipt"]
        if ref["origin"] != "bundle":
            raise ManifestError("Python lock reconstruction receipt must originate in bundle")
        raw = _read_reference(
            ref, f"lock reconstruction {key[0]}", source, bundle, tracked, source_commit
        )
        receipt = _strict_json_object(raw, f"lock reconstruction {key[0]} receipt")
        if set(receipt) != receipt_keys:
            raise ManifestError("Python lock reconstruction receipt has unexpected fields")
        if receipt["observed_runtime"] != {
            "implementation": "cpython",
            "version": runtime["version"],
        }:
            raise ManifestError("Python lock reconstruction observed runtime mismatch")
        identity_fields = {
            "schema_version": 1,
            "contract_type": "python_lock_reconstruction_v1",
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "source_commit": source_commit,
            "lock_id": lock["id"],
            "lock_sha256": lock["file"]["sha256"],
            "environment": row["environment"],
            "python_runtime_id": runtime["id"],
        }
        if any(receipt.get(field) != value for field, value in identity_fields.items()):
            raise ManifestError("Python lock reconstruction receipt identity mismatch")
        if isinstance(receipt["schema_version"], bool) or not isinstance(
            receipt["schema_version"], int
        ):
            raise ManifestError("Python lock reconstruction receipt schema version is invalid")
        if (
            not isinstance(receipt["run_id"], str)
            or not receipt["run_id"].strip()
            or isinstance(receipt["attempt"], bool)
            or not isinstance(receipt["attempt"], int)
            or receipt["attempt"] < 1
            or not isinstance(receipt["outcome"], str)
            or receipt["outcome"] not in {"passed", "failed", "blocked", "canceled"}
        ):
            raise ManifestError("Python lock reconstruction receipt run identity is invalid")
        normalized = _normalized_distributions(
            receipt["resolved_distributions"], f"lock {lock['id']}"
        )
        candidate_names = {canonicalize_name(item) for item in lock["covered_components"]}
        if any(name in candidate_names for name, _ in normalized):
            raise ManifestError("Python lock reconstruction includes a candidate distribution")
        expected_external = tuple(
            pair for pair in normalized_lock_pins[lock["id"]] if pair[0] not in candidate_names
        )
        if normalized != expected_external:
            raise ManifestError(
                f"Python lock reconstruction inventory differs from exact lock pins: {lock['id']}"
            )
        rows[key] = receipt
        receipt_hashes.add(ref["sha256"])
    if set(rows) != expected:
        raise ManifestError("Python locks lack exact runtime reconstruction receipts")

    # The release contract requires the same reconstructed third-party
    # inventory for each lock purpose and target environment on both supported
    # Python minors. Markers may affect source requirement evaluation for an
    # exact runtime, but they do not relax this cross-minor parity guarantee.
    comparable: dict[tuple[str, str], dict[str, tuple[tuple[str, str], ...]]] = {}
    for lock in python_locks:
        runtime = runtimes[lock["python_runtime_id"]]
        version = Version(runtime["version"])
        minor = f"{version.release[0]}{version.release[1]}"
        key = (lock["purpose"], lock["target_environment"])
        reconstruction_key = (lock["id"], runtime["id"], lock["target_environment"])
        normalized = _normalized_distributions(
            rows[reconstruction_key]["resolved_distributions"],
            f"lock {lock['id']} reconstruction",
        )
        comparable.setdefault(key, {})[minor] = normalized
    for (purpose, environment), inventories in comparable.items():
        if set(inventories) != {"312", "313"}:
            raise ManifestError(
                "Python lock reconstruction parity requires corresponding 3.12 and 3.13 "
                f"locks for {purpose} / {environment}"
            )
        if inventories["312"] != inventories["313"]:
            raise ManifestError(
                "Python lock reconstruction inventories differ across Python minors: "
                f"{purpose} / {environment}"
            )
    return rows, receipt_hashes


def _hashed_lock_artifacts(content: bytes, label: str) -> dict[str, tuple[str, set[str]]]:
    """Return normalized exact pins and allowed SHA-256 artifact hashes."""
    entries: dict[str, tuple[str, set[str]]] = {}
    for line in _requirements_logical_lines(content, label):
        hashes, requirement_text = _extract_requirement_hashes(line)
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise ManifestError(f"{label} requirement is invalid") from exc
        if len(requirement.specifier) != 1 or next(iter(requirement.specifier)).operator != "==":
            raise ManifestError(f"{label} requirement is not an exact pin")
        name = canonicalize_name(requirement.name)
        version = canonicalize_version(
            Version(next(iter(requirement.specifier)).version), strip_trailing_zero=True
        )
        entries[name] = (version, {value.lower() for value in hashes})
    return entries


def _same_normalized_version(left: str, right: str) -> bool:
    try:
        return canonicalize_version(
            Version(left), strip_trailing_zero=True
        ) == canonicalize_version(Version(right), strip_trailing_zero=True)
    except InvalidVersion:
        return False


def _validate_preserved_bootstrap_hashes(
    bootstrap_entries: dict[str, tuple[str, set[str]]],
    final_entries: dict[str, tuple[str, set[str]]],
) -> None:
    for name, (version, hashes) in bootstrap_entries.items():
        final_entry = final_entries.get(name)
        if final_entry is None or final_entry[0] != version or hashes != final_entry[1]:
            raise ManifestError("final build lock changes or omits bootstrap package hashes")


def _validate_accepted_build_coverage(
    components: list[dict[str, Any]],
    runtime_ids: set[str],
    target_environments: set[str],
    builds: dict[tuple[str, str, str, str], dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> None:
    for component in components:
        if component["kind"] != "python_distribution":
            continue
        for runtime_id in runtime_ids:
            for target_environment in target_environments:
                for operation in ("wheel", "sdist"):
                    attestation = builds.get(
                        (component["id"], runtime_id, target_environment, operation)
                    )
                    if attestation is None or attestation["outcome"] != "passed":
                        raise ManifestError(
                            "accepted decision lacks passing typed "
                            f"{operation} build evidence for {component['id']} / "
                            f"{runtime_id} / {target_environment}"
                        )
                    artifact_sha = attestation["_artifact_sha256"]
                    if not any(
                        artifact["component"] == component["id"]
                        and artifact["kind"] == "distribution"
                        and artifact["file"]["sha256"] == artifact_sha
                        for artifact in artifacts
                    ):
                        raise ManifestError(
                            "accepted build attestation artifact is not in candidate"
                        )


def _validate_build_attestation_links(
    row: dict[str, Any], context: dict[str, Any], hook: dict[str, Any], lock: dict[str, Any]
) -> None:
    if (
        hook["context_sha256"] != row["context_sha256"]
        or hook["operation"] != row["operation"]
        or hook["status"] not in {"returned", "optional_hook_absent"}
    ):
        raise ManifestError("build attestation context/hook/operation mismatch")
    if (
        lock["purpose"] != "build"
        or lock["id"] != row["final_lock_id"]
        or lock["file"]["sha256"] != row["final_lock_sha256"]
        or row["final_lock_id"] != hook["final_lock_id"]
        or row["final_lock_sha256"] != hook["final_lock_sha256"]
        or row["reconstruction_receipt_sha256"] != hook["reconstruction_receipt_sha256"]
    ):
        raise ManifestError("build attestation final lock differs from hook attestation")
    if (
        row["python_runtime_id"] != context["python_runtime_id"]
        or row["target_environment"] != context["target"]["environment"]
        or lock.get("python_runtime_id") != row["python_runtime_id"]
        or lock["target_environment"] != row["target_environment"]
        or context["component_id"] not in lock["covered_components"]
    ):
        raise ManifestError("build attestation runtime/target differs from build context")


def _validate_build_evidence(
    payload: dict[str, Any],
    source: Path,
    bundle: Path,
    tracked: set[str],
    source_commit: str,
    components: list[dict[str, Any]],
    locks: list[dict[str, Any]],
    runtimes: list[dict[str, Any]],
    lock_pins: dict[str, tuple[tuple[str, str], ...]],
    reconstructions: dict[tuple[str, str, str], dict[str, Any]],
    *,
    portable_wheel_policy: bool = False,
    source_python_specifiers: dict[str, SpecifierSet] | None = None,
) -> None:
    """Cross-check typed PEP 517 evidence; this validates claims, not execution trust."""
    context_rows = payload.get("build_contexts", [])
    hook_rows = payload.get("hook_attestations", [])
    build_rows = payload.get("build_attestations", [])
    contexts = {row["context_sha256"]: row for row in context_rows}
    if len(contexts) != len(context_rows):
        raise ManifestError("duplicate build context identity")
    component_map = {row["id"]: row for row in components}
    lock_map = {row["id"]: row for row in locks}
    runtime_ids = {row["id"] for row in runtimes}
    for digest, row in contexts.items():
        _check_identity(row, "context_sha256", "build context")
        component = component_map.get(row["component_id"])
        if component is None or component["kind"] != "python_distribution":
            raise ManifestError("build context references unknown Python component")
        if (
            row["source_commit"] != source_commit
            or row["component_version"] != component["version"]
        ):
            raise ManifestError("build context source/component identity mismatch")
        if row["descriptor"] != component["descriptor"] or row["descriptor"]["origin"] != "source":
            raise ManifestError("build context descriptor differs from candidate source descriptor")
        descriptor = _read_reference(
            row["descriptor"], "build context descriptor", source, bundle, tracked, source_commit
        )
        try:
            build_system = tomllib.loads(descriptor.decode("utf-8"))["build-system"]
        except (UnicodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise ManifestError(
                "build context descriptor has invalid build-system metadata"
            ) from exc
        if row["backend"]["entrypoint"] != build_system.get("build-backend"):
            raise ManifestError("build context backend differs from source descriptor")
        source_backend_path = build_system.get("backend-path", [])
        if len(row["backend_path"]) != len(source_backend_path):
            raise ManifestError("build context backend-path differs from source descriptor")
        for path_record, relative in zip(row["backend_path"], source_backend_path, strict=True):
            directory = PurePosixPath(path_record["directory"])
            if directory.is_absolute() or ".." in directory.parts or "\\" in str(directory):
                raise ManifestError("build context backend-path directory is unsafe")
            expected_directory = PurePosixPath(component["descriptor"]["path"]).parent / relative
            if directory != expected_directory:
                raise ManifestError("build context backend-path identity mismatch")
            expected_files = {
                path for path in tracked if PurePosixPath(path).is_relative_to(directory)
            }
            actual_files = {ref["path"] for ref in path_record["files"]}
            if not expected_files or actual_files != expected_files:
                raise ManifestError("build context backend-path file inventory is incomplete")
            for reference in path_record["files"]:
                if reference["origin"] != "source":
                    raise ManifestError("build context backend-path files must originate in source")
                _check_reference(
                    reference, "build context backend-path", source, bundle, tracked, source_commit
                )
        if row["python_runtime_id"] not in runtime_ids:
            raise ManifestError("build context references undeclared Python runtime")
        boot = lock_map.get(row["bootstrap_lock_id"])
        if (
            boot is None
            or boot["purpose"] != "bootstrap"
            or component["id"] not in boot["covered_components"]
            or boot["file"]["sha256"] != row["bootstrap_lock_sha256"]
        ):
            raise ManifestError("build context bootstrap lock identity mismatch")
        if (
            boot["python_runtime_id"] != row["python_runtime_id"]
            or boot["target_environment"] != row["target"]["environment"]
        ):
            raise ManifestError("build context bootstrap lock applicability mismatch")
        if row["environment"]["identity"] != boot["target_environment"]:
            raise ManifestError("build context environment differs from bootstrap lock target")
        bootstrap_receipt_row = next(
            (
                r
                for r in payload["lock_reconstructions"]
                if r["lock_id"] == boot["id"]
                and r["python_runtime_id"] == row["python_runtime_id"]
                and r["environment"] == row["environment"]["identity"]
            ),
            None,
        )
        if (
            bootstrap_receipt_row is None
            or bootstrap_receipt_row["receipt"]["sha256"]
            != row["bootstrap_reconstruction_receipt_sha256"]
        ):
            raise ManifestError("build context bootstrap reconstruction receipt mismatch")
        bootstrap_receipt = reconstructions.get(
            (boot["id"], row["python_runtime_id"], row["environment"]["identity"])
        )
        if bootstrap_receipt is None or bootstrap_receipt["outcome"] != "passed":
            raise ManifestError("build context bootstrap environment lacks passed reconstruction")
        bootstrap_content = _read_reference(
            boot["file"], "build context bootstrap lock", source, bundle, tracked, source_commit
        )
        bootstrap_entries = _hashed_lock_artifacts(
            bootstrap_content, "build context bootstrap lock"
        )
        backend_name = canonicalize_name(row["backend"]["distribution"])
        backend_entry = bootstrap_entries.get(backend_name)
        if backend_entry is None or row["backend"]["artifact_sha256"] not in backend_entry[1]:
            raise ManifestError("build context backend artifact is not pinned by bootstrap lock")
        if backend_entry is None or not _same_normalized_version(
            backend_entry[0], row["backend"]["version"]
        ):
            raise ManifestError("build context backend version differs from bootstrap lock")
        for tool in row["toolchain"]:
            entry = bootstrap_entries.get(canonicalize_name(tool["name"]))
            if (
                entry is None
                or tool["artifact_sha256"].lower() not in entry[1]
                or not _same_normalized_version(entry[0], tool["version"])
            ):
                raise ManifestError("build context toolchain is not pinned by bootstrap lock")
        frontend_entry = bootstrap_entries.get(canonicalize_name(row["frontend"]["name"]))
        frontend_tool = next(
            (
                item
                for item in row["toolchain"]
                if canonicalize_name(item["name"]) == canonicalize_name(row["frontend"]["name"])
            ),
            None,
        )
        if (
            frontend_entry is None
            or not _same_normalized_version(frontend_entry[0], row["frontend"]["version"])
            or frontend_tool is None
            or frontend_tool["artifact_sha256"].lower() not in frontend_entry[1]
        ):
            raise ManifestError("build frontend is not pinned by bootstrap lock/toolchain")
    hooks: dict[str, dict[str, Any]] = {}
    for row in hook_rows:
        digest = _check_identity(row, "attestation_sha256", "hook attestation")
        context = contexts.get(row["context_sha256"])
        if context is None:
            raise ManifestError("hook attestation references unknown build context")
        key = f"{digest}"
        if key in hooks:
            raise ManifestError("duplicate hook attestation identity")
        returned_requirements = row.get("returned_requirements", [])
        if (
            row["status"] in {"failed", "unavailable", "optional_hook_absent"}
            and returned_requirements
        ):
            raise ManifestError("failed/unavailable hook cannot claim returned requirements")
        if row["status"] not in {"failed", "unavailable"}:
            lock = lock_map.get(row["final_lock_id"])
            if (
                lock is None
                or lock["purpose"] != "build"
                or lock["file"]["sha256"] != row["final_lock_sha256"]
            ):
                raise ManifestError("hook attestation final build lock mismatch")
            if (
                lock.get("python_runtime_id") != context["python_runtime_id"]
                or context["component_id"] not in lock["covered_components"]
                or lock["target_environment"] != context["target"]["environment"]
            ):
                raise ManifestError("hook attestation final build lock applicability mismatch")
            receipt_key = (lock["id"], context["python_runtime_id"], lock["target_environment"])
            receipt = reconstructions.get(receipt_key)
            if (
                receipt is None
                or receipt["outcome"] != "passed"
                or row["reconstruction_receipt_sha256"]
                != next(
                    (
                        r["receipt"]["sha256"]
                        for r in payload["lock_reconstructions"]
                        if (r["lock_id"], r["python_runtime_id"], r["environment"]) == receipt_key
                    ),
                    None,
                )
            ):
                raise ManifestError(
                    "hook attestation lacks matching passed final-lock reconstruction"
                )
            pins = dict(lock_pins[lock["id"]])
            bootstrap_lock = lock_map[context["bootstrap_lock_id"]]
            bootstrap_content = _read_reference(
                bootstrap_lock["file"], "bootstrap lock", source, bundle, tracked, source_commit
            )
            bootstrap_entries = _hashed_lock_artifacts(bootstrap_content, "bootstrap lock")
            final_entries = _hashed_lock_artifacts(
                _read_reference(
                    lock["file"], "final build lock", source, bundle, tracked, source_commit
                ),
                "final build lock",
            )
            _validate_preserved_bootstrap_hashes(bootstrap_entries, final_entries)
            _validate_hook_requirement_pins(returned_requirements, pins)
            for name, version in lock_pins[lock["id"]]:
                if name in {canonicalize_name(x) for x in component_map}:
                    raise ManifestError(
                        "internal build requirements remain unsupported before exact candidate artifact installation"
                    )
        hooks[digest] = row
    builds: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in build_rows:
        digest = _check_identity(row, "attestation_sha256", "build attestation")
        context = contexts.get(row["context_sha256"])
        hook = hooks.get(row["hook_attestation_sha256"])
        lock = lock_map.get(row["final_lock_id"])
        if context is None or hook is None or lock is None:
            raise ManifestError("build attestation references unknown context, hook, or lock")
        _validate_build_attestation_links(row, context, hook, lock)
        if row["outcome"] == "passed" and context["environment"]["isolation"] != "isolated":
            raise ManifestError("passing build attestation requires isolated build context")
        ref = row.get("artifact")
        if row["outcome"] == "passed":
            if ref is None or ref["origin"] != "bundle":
                raise ManifestError(
                    "passing build attestation must bind a candidate bundle artifact"
                )
            content = _check_reference(
                ref, "build attestation artifact", source, bundle, tracked, source_commit
            )
            artifact = next(
                (
                    a
                    for a in payload["candidate"]["artifacts"]
                    if a["component"] == context["component_id"]
                    and a["file"]["sha256"] == ref["sha256"]
                ),
                None,
            )
            if artifact is None or artifact["kind"] != "distribution":
                raise ManifestError(
                    "build attestation artifact is not bound to its candidate component"
                )
            artifact_format = _validate_python_distribution_artifact(
                ref["path"],
                content,
                component_map[context["component_id"]],
                validate_portable_policy=portable_wheel_policy,
                runtime_versions=tuple(runtime["version"] for runtime in runtimes),
                source_python_specifier=(source_python_specifiers or {}).get(
                    canonicalize_name(context["component_id"])
                ),
            )
            if artifact_format != row["operation"]:
                raise ManifestError(
                    "build attestation operation differs from resulting artifact format"
                )
            artifact_sha256 = ref["sha256"]
        else:
            if ref is not None:
                raise ManifestError("failed/unavailable build attestation cannot claim an artifact")
            artifact_sha256 = ""
        key = (
            context["component_id"],
            row["python_runtime_id"],
            row["target_environment"],
            row["operation"],
        )
        if key in builds:
            raise ManifestError(
                "duplicate build attestation for component/runtime/target/operation"
            )
        builds[key] = {**row, "_artifact_sha256": artifact_sha256}
    if payload["release_decision"]["decision"] == "accepted":
        _validate_accepted_build_coverage(
            components,
            runtime_ids,
            {row["target_environment"] for row in locks if row["purpose"] == "build"},
            builds,
            payload["candidate"]["artifacts"],
        )


def _validate_hook_requirement_pins(requirements: list[str], pins: dict[str, str]) -> None:
    """Require every operation-specific backend requirement to match a final lock pin."""
    for text in requirements:
        try:
            req = Requirement(text)
        except InvalidRequirement as exc:
            raise ManifestError("hook requirement is invalid") from exc
        if req.marker is not None or req.url is not None or req.extras:
            raise ManifestError("hook requirements cannot use markers, URLs, or extras")
        pin = pins.get(canonicalize_name(req.name))
        if pin is None or not req.specifier.contains(Version(pin)):
            raise ManifestError(f"final build lock omits or violates hook requirement: {req.name}")


def _validate_candidate_pipeline_and_report(
    payload: dict[str, Any],
    source_root: str | Path,
    candidate_bundle_root: str | Path,
    *,
    policy: _ManifestPolicy,
) -> ObligationReport:
    """Run shared structural checkpoints under a pinned version policy."""
    source = Path(source_root).resolve(strict=True)
    bundle = Path(candidate_bundle_root).resolve(strict=True)
    if not source.is_dir() or not bundle.is_dir():
        raise ManifestError("source_root and candidate_bundle_root must be directories")
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    tracked = _tracked_paths(source)

    # Validate the untrusted payload before indexing into it. The schema comes
    # from clean current HEAD, and the manifest's source commit must equal that
    # same HEAD before candidate references are processed.
    trusted_head = _git(source, "rev-parse", "HEAD")
    _clean_exact_source(source, trusted_head)
    schema_digest = _schema_validate(payload, source, bundle, tracked, trusted_head, policy)
    from scripts.validated_release_facts import (
        AcceptanceRequirementFact,
        ArtifactFact,
        BuildContextFact,
        BuildFact,
        CandidateFacts,
        CompatibilityProviderFact,
        CompatibilitySubjectFact,
        ComponentFact,
        EvidenceFact,
        HookFact,
        LockFact,
        LockRequirementFact,
        MigrationRollbackFact,
        PolicyFacts,
        ReconstructionFact,
        ReferenceCollector,
        RelationFact,
        RuntimeScopeFact,
        ValidatedReleaseFacts,
    )

    reference_collector = ReferenceCollector()
    web_console_facts = None
    reference_collector.capture(
        "manifest_schema",
        {
            "origin": "source",
            "path": policy.schema_path.as_posix(),
            "sha256": schema_digest,
        },
    )

    candidate = payload["candidate"]
    profile = payload["support_profile"]
    candidate_id = _check_identity(candidate, "candidate_id", "candidate_id")
    profile_id = _check_identity(profile, "profile_id", "profile_id")
    _canonical_repository_uri(candidate["source"]["repository"])
    source_commit = candidate["source"]["commit"]
    if source_commit != trusted_head:
        raise ManifestError("candidate source commit must equal repository HEAD")

    components = candidate["components"]
    python_runtimes = profile["python_runtimes"]
    if len(profile["target_environments"]) != 1:
        raise ManifestError("support profile must declare exactly one target environment")
    if len(python_runtimes) != 2:
        raise ManifestError("support profile must declare exact CPython 3.12 and 3.13 runtimes")
    runtime_minors: set[tuple[int, int]] = set()
    runtime_ids: set[str] = set()
    for runtime in python_runtimes:
        runtime_id, version = runtime["id"], runtime["version"]
        try:
            parsed = Version(version)
        except InvalidVersion as exc:
            raise ManifestError("Python runtime version must be exact stable X.Y.Z") from exc
        if (
            not parsed.release
            or len(parsed.release) != 3
            or parsed.pre is not None
            or parsed.post is not None
            or parsed.dev is not None
            or parsed.local is not None
            or str(parsed) != version
            or runtime_id != f"cpython-{version}"
        ):
            raise ManifestError(
                "Python runtime must use exact stable CPython identity cpython-X.Y.Z"
            )
        runtime_minors.add((parsed.release[0], parsed.release[1]))
        runtime_ids.add(runtime_id)
    if runtime_minors != {(3, 12), (3, 13)} or len(runtime_ids) != 2:
        raise ManifestError("support profile must cover CPython minors 3.12 and 3.13 exactly")
    source_python_specifiers = _check_component_inventory(
        source, bundle, components, tracked, source_commit, python_runtimes
    )
    for component in components:
        reference_collector.capture(
            f"component:{component['id']}:descriptor", component["descriptor"]
        )
        for source_ref in component["source_files"]:
            reference_collector.capture(
                f"component:{component['id']}:source:{source_ref['path']}", source_ref
            )
    (
        runtime_requirement_policies,
        build_requirement_policies,
        selected_runtime_requirements,
        python_descriptor_refs,
        python_selection_digest,
    ) = _source_python_requirement_policies(
        source,
        bundle,
        components,
        tracked,
        source_commit,
        python_runtimes,
        profile.get("python_dependency_selection") if policy.schema_version == 3 else None,
    )
    component_ids = {item["id"] for item in components}
    python_ids = {item["id"] for item in components if item["kind"] == "python_distribution"}
    console_ids = {item["id"] for item in components if item["kind"] == "web_console"}
    identity_groups = (
        ("component", components),
        ("lock", candidate["locks"]),
        ("compatibility", candidate["compatibility"]),
        ("migration", candidate["migrations"]),
        ("rollback", candidate["rollback"]),
    )
    for label, rows in identity_groups:
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise ManifestError(f"duplicate {label} id")

    component_kinds = {item["id"]: item["kind"] for item in components}
    components_by_id = {item["id"]: item for item in components}
    components_by_normalized_name = {
        canonicalize_name(item["id"]): item
        for item in components
        if item["kind"] == "python_distribution"
    }
    target_environments = set(profile["target_environments"])
    requirement_tuples: list[tuple[str, str, str, str, str | None]] = []
    for requirement in profile["lock_requirements"]:
        component_id = requirement["component_id"]
        environment = requirement["target_environment"]
        if component_id not in component_ids:
            raise ManifestError(f"lock requirement references unknown component: {component_id}")
        if environment not in target_environments:
            raise ManifestError(f"lock requirement targets undeclared environment: {environment}")
        runtime_id = requirement.get("python_runtime_id")
        if requirement["ecosystem"] == "python":
            if runtime_id not in runtime_ids:
                raise ManifestError("Python lock requirement references undeclared runtime")
        elif runtime_id is not None:
            raise ManifestError("non-Python lock requirement cannot declare a Python runtime")
        requirement_tuples.append(
            (
                component_id,
                requirement["ecosystem"],
                requirement["purpose"],
                environment,
                runtime_id,
            )
        )
    if len(requirement_tuples) != len(set(requirement_tuples)):
        raise ManifestError("duplicate lock requirement tuple")
    required_environments = {item[3] for item in requirement_tuples}
    if target_environments - required_environments:
        raise ManifestError(
            f"target environments lack lock requirements: {sorted(target_environments - required_environments)}"
        )

    no_lock_rows = profile["no_lock_components"]
    no_lock_components = [row["component_id"] for row in no_lock_rows]
    if any(not row["rationale"].strip() for row in no_lock_rows):
        raise ManifestError("no-lock declaration rationale must be nonempty")
    if len(no_lock_components) != len(set(no_lock_components)):
        raise ManifestError("duplicate no-lock component declaration")
    unknown_no_lock = set(no_lock_components) - component_ids
    if unknown_no_lock:
        raise ManifestError(
            f"no_lock_components references unknown components: {sorted(unknown_no_lock)}"
        )
    for component_id in no_lock_components:
        if component_kinds[component_id] not in {
            "operator_ui",
            "recovery_ui",
            "service",
            "runbook",
        }:
            raise ManifestError(f"component kind cannot be declared no-lock: {component_id}")
        if any(row[0] == component_id for row in requirement_tuples):
            raise ManifestError(f"no-lock component also has lock requirements: {component_id}")

    covered_components = {row[0] for row in requirement_tuples}
    if covered_components & set(no_lock_components):
        raise ManifestError("component has both lock applicability and no-lock declaration")
    if component_ids != covered_components | set(no_lock_components):
        uncovered = component_ids - covered_components - set(no_lock_components)
        raise ManifestError(
            f"components lack lock applicability or explicit no-lock classification: {sorted(uncovered)}"
        )

    for component_id, kind in component_kinds.items():
        rows = {row[2] for row in requirement_tuples if row[0] == component_id}
        if kind == "python_distribution":
            applicable_environments = {
                row[3] for row in requirement_tuples if row[0] == component_id
            }
            if not applicable_environments:
                raise ManifestError(f"Python component {component_id} lacks lock applicability")
            for environment in applicable_environments:
                for runtime_id in runtime_ids:
                    if not {
                        (component_id, "python", "build", environment, runtime_id),
                        (component_id, "python", "runtime", environment, runtime_id),
                    } <= set(requirement_tuples):
                        raise ManifestError(
                            f"Python component {component_id} requires Python build and runtime locks for {runtime_id} in {environment}"
                        )
        if kind == "web_console" and ("node", "build") not in {
            (row[1], row[2]) for row in requirement_tuples if row[0] == component_id
        }:
            raise ManifestError(f"Web Console component {component_id} requires a Node build lock")

    lock_tuples: list[tuple[str, str, str, str, str | None]] = []
    python_locks = [item for item in candidate["locks"] if item["ecosystem"] == "python"]
    expected_python_lock_ids = {
        f"python-{purpose}-{minor}"
        for purpose in ("bootstrap", "build", "runtime")
        for minor in ("312", "313")
    }
    python_lock_combinations: set[tuple[str, tuple[int, int]]] = set()
    normalized_lock_pins: dict[str, tuple[tuple[str, str], ...]] = {}
    for lock in candidate["locks"]:
        if not set(lock["covered_components"]) <= component_ids:
            raise ManifestError(f"lock {lock['id']} references unknown component")
        for component_id in lock["covered_components"]:
            kind = component_kinds[component_id]
            runtime_id = lock.get("python_runtime_id")
            if lock["ecosystem"] == "python":
                if runtime_id not in runtime_ids:
                    raise ManifestError(f"Python lock {lock['id']} references undeclared runtime")
            elif runtime_id is not None:
                raise ManifestError(f"non-Python lock {lock['id']} cannot declare a Python runtime")
            if lock["ecosystem"] == "python" and kind != "python_distribution":
                raise ManifestError(f"Python lock {lock['id']} covers a non-Python component")
            if lock["ecosystem"] == "node" and kind != "web_console":
                raise ManifestError(f"Node lock {lock['id']} covers a non-console component")
            if kind == "python_distribution" and lock["ecosystem"] != "python":
                raise ManifestError(
                    f"Python component {component_id} requires Python ecosystem locks"
                )
            if kind == "web_console" and lock["ecosystem"] != "node":
                raise ManifestError(
                    f"Web Console component {component_id} requires Node ecosystem locks"
                )
            lock_tuples.append(
                (
                    component_id,
                    lock["ecosystem"],
                    lock["purpose"],
                    lock["target_environment"],
                    runtime_id,
                )
            )
        if lock["target_environment"] not in profile["target_environments"]:
            raise ManifestError(f"lock {lock['id']} targets an undeclared environment")
        if lock["ecosystem"] == "python":
            runtime = next(
                item for item in python_runtimes if item["id"] == lock["python_runtime_id"]
            )
            version = Version(runtime["version"])
            minor = f"{version.release[0]}{version.release[1]}"
            expected_lock_id = f"python-{lock['purpose']}-{minor}"
            if lock["id"] != expected_lock_id:
                raise ManifestError(
                    f"Python lock {lock['id']} must identify its structured purpose and runtime as {expected_lock_id}"
                )
            python_lock_combinations.add(
                (lock["purpose"], (version.release[0], version.release[1]))
            )
        lock_content = _check_reference(
            lock["file"], f"lock {lock['id']}", source, bundle, tracked, source_commit
        )
        reference_collector.capture(f"lock:{lock['id']}", lock["file"])
        parsed_pins = _validate_lock_content(
            lock, lock_content, components_by_id, source, bundle, tracked, source_commit
        )
        if parsed_pins is not None:
            source_policy_by_runtime = (
                runtime_requirement_policies
                if lock["purpose"] == "runtime"
                else build_requirement_policies
            )
            runtime_id = lock.get("python_runtime_id")
            source_policy = {
                component_id: runtime_policies.get(runtime_id, {})
                for component_id, runtime_policies in source_policy_by_runtime.items()
            }
            _validate_source_requirement_coverage(
                parsed_pins,
                lock["covered_components"],
                source_policy,
                components_by_normalized_name,
                f"lock {lock['id']}",
                purpose=lock["purpose"],
                runtime_lock_coverage=set(requirement_tuples),
                environment=lock["target_environment"],
                runtime_id=lock.get("python_runtime_id"),
            )
            if policy.schema_version == 3 and lock["purpose"] == "runtime":
                runtime = next(
                    item for item in python_runtimes if item["id"] == lock["python_runtime_id"]
                )
                _validate_python_runtime_resolution_receipt(
                    lock,
                    runtime,
                    profile["python_dependency_selection"],
                    python_selection_digest or "",
                    selected_runtime_requirements,
                    python_descriptor_refs,
                    source,
                    bundle,
                    tracked,
                    source_commit,
                    reference_collector,
                )
            normalized_lock_pins[lock["id"]] = parsed_pins
    if len(lock_tuples) != len(set(lock_tuples)):
        raise ManifestError("multiple locks cover the same component applicability tuple")
    if set(lock_tuples) - set(requirement_tuples):
        raise ManifestError("lock coverage tuple is absent from profile lock_requirements")
    if set(requirement_tuples) - set(lock_tuples):
        raise ManifestError("profile lock requirement does not match exactly one lock")
    if {item["id"] for item in python_locks} != expected_python_lock_ids:
        raise ManifestError("Python locks must use the six required bootstrap/build/runtime IDs")
    if python_lock_combinations != {
        (purpose, minor)
        for purpose in ("bootstrap", "build", "runtime")
        for minor in ((3, 12), (3, 13))
    }:
        raise ManifestError("Python lock purposes and structured runtimes must cover both minors")

    reconstructions, reconstruction_hashes = _validate_lock_reconstructions(
        payload,
        source,
        bundle,
        tracked,
        source_commit,
        candidate_id,
        profile_id,
        python_locks,
        python_runtimes,
        normalized_lock_pins,
    )

    for index, component in enumerate(components):
        if component["descriptor"]["origin"] != "source":
            raise ManifestError(f"component {component['id']} descriptor must originate in source")
        for source_ref in component["source_files"]:
            if source_ref["origin"] != "source":
                raise ManifestError(
                    f"component {component['id']} source file must originate in source"
                )
    artifact_components: set[str] = set()
    python_artifact_formats: dict[str, set[str]] = {
        component_id: set() for component_id in python_ids
    }
    for artifact in candidate["artifacts"]:
        if artifact["component"] not in component_ids:
            raise ManifestError("artifact references unknown component")
        artifact_components.add(artifact["component"])
        if artifact["file"]["origin"] != "bundle":
            raise ManifestError("built artifacts must originate in the candidate bundle")
        component = components_by_id[artifact["component"]]
        max_bytes = None
        if component["kind"] == "python_distribution":
            if artifact["kind"] != "distribution":
                raise ManifestError(
                    f"Python component {component['id']} artifacts must use distribution kind"
                )
            max_bytes = MAX_DISTRIBUTION_ARCHIVE_BYTES
        if component["kind"] == "python_distribution":
            artifact_content = _read_reference(
                artifact["file"],
                "artifact",
                source,
                bundle,
                tracked,
                source_commit,
                max_bytes,
            )
            artifact_format = _validate_python_distribution_artifact(
                artifact["file"]["path"],
                artifact_content,
                component,
                validate_portable_policy=(
                    policy.schema_version == 3 and profile.get("name") == "portable_python3_v1"
                ),
                runtime_versions=tuple(runtime["version"] for runtime in python_runtimes),
                source_python_specifier=source_python_specifiers.get(
                    canonicalize_name(component["id"])
                ),
            )
            python_artifact_formats[component["id"]].add(artifact_format)
        else:
            _verify_reference_digest(
                artifact["file"], "artifact", source, bundle, tracked, source_commit
            )
        reference_collector.capture(
            f"artifact:{artifact['component']}:{artifact['file']['path']}", artifact["file"]
        )
    if component_ids - artifact_components:
        raise ManifestError(
            f"candidate bundle lacks artifacts for components: {sorted(component_ids - artifact_components)}"
        )
    _validate_build_evidence(
        payload,
        source,
        bundle,
        tracked,
        source_commit,
        components,
        python_locks,
        python_runtimes,
        normalized_lock_pins,
        reconstructions,
        portable_wheel_policy=(
            policy.schema_version == 3 and profile.get("name") == "portable_python3_v1"
        ),
        source_python_specifiers=source_python_specifiers,
    )
    if payload["release_decision"]["decision"] == "accepted":
        incomplete = sorted(
            component_id
            for component_id, formats in python_artifact_formats.items()
            if not {"wheel", "sdist"} <= formats
        )
        if incomplete:
            raise ManifestError(
                f"accepted candidate lacks a valid Python wheel and sdist for: {incomplete}"
            )

    known_gates: set[str] = set()
    for lock in python_locks:
        known_gates.update({f"lock:{lock['id']}", f"rebuild:{lock['id']}"})
    _validate_compatibility_contracts(
        candidate["compatibility"],
        source,
        bundle,
        tracked,
        source_commit,
        candidate_artifacts=candidate["artifacts"],
        components_by_id=components_by_id,
    )
    for item in candidate["compatibility"]:
        if item["evidence_required"]:
            known_gates.add(f"compatibility:{item['id']}")
    for item in candidate["migrations"]:
        _check_reference(
            item["implementation"],
            f"migration {item['id']}",
            source,
            bundle,
            tracked,
            source_commit,
        )
        known_gates.add(f"migration:{item['id']}")
    for item in candidate["rollback"]:
        _check_reference(
            item["procedure"], f"rollback {item['id']}", source, bundle, tracked, source_commit
        )
        known_gates.add(f"rollback:{item['id']}")
    _check_reference(
        candidate["composition"], "composition", source, bundle, tracked, source_commit
    )
    for name in ("topology", "host_profile", "acceptance_policy"):
        _check_reference(profile[name], name, source, bundle, tracked, source_commit)
        if name == "acceptance_policy":
            reference_collector.capture(name, profile[name])
    for capability_name, capability in profile["capabilities"].items():
        unknown = set(capability["components"]) - component_ids
        if unknown:
            raise ManifestError(
                f"capability {capability_name} references unknown components: {sorted(unknown)}"
            )
        known_gates.update(capability["required_gates"])
    if set(profile["capabilities"]["python_distributions"]["components"]) != python_ids:
        raise ManifestError("python_distributions capability must cover all 26 packages exactly")
    if profile["capabilities"]["market_data_web_console"]["components"] != sorted(console_ids):
        raise ManifestError("market_data_web_console capability must cover the console package")

    inventory_roles, component_inventory_digest = _load_component_inventory(
        profile["component_inventory"], source, bundle, tracked, source_commit, policy
    )
    reference_collector.capture("component_inventory", profile["component_inventory"])
    coverage_policy, compatibility_policy_digest = _load_compatibility_coverage_policy(
        source, bundle, tracked, source_commit
    )
    reference_collector.capture(
        "compatibility_coverage_policy",
        {
            "origin": "source",
            "path": "schemas/platform-compatibility-coverage-policy-v1.json",
            "sha256": compatibility_policy_digest,
        },
    )
    provider_gate_pairs, unresolved_roles = _validate_inventory_relationships(
        inventory_roles, components, profile["capabilities"], known_gates
    )
    compatibility_gates: dict[str, str] = {}
    for role in inventory_roles:
        if role["capability"] not in coverage_policy["required_capabilities"]:
            continue
        for component_id in role["providers"]:
            gate = _compatibility_provider_gate(component_id, coverage_policy)
            compatibility_gates[component_id] = gate
            known_gates.add(gate)
            provider_gate_pairs.add((gate, component_id))
    acceptance_rows = profile["acceptance_requirements"]
    components_by_id = {item["id"]: item for item in components}
    required_compatibility_scopes = {
        component_id: _compatibility_scope_keys(
            component_id, components_by_id, profile, acceptance_rows, compatibility_gates
        )
        for component_id in compatibility_gates
    }
    _validate_compatibility_coverage(
        candidate["compatibility_coverage"],
        inventory_roles,
        provider_gate_pairs,
        components,
        candidate,
        required_compatibility_scopes,
        accepted=payload["release_decision"]["decision"] == "accepted",
    )
    acceptance_tuples = [
        (row["gate"], row["component_id"], row["environment"]) for row in acceptance_rows
    ]
    if len(acceptance_tuples) != len(set(acceptance_tuples)):
        raise ManifestError("duplicate acceptance requirement tuple")
    acceptance_set = set(acceptance_tuples)
    acceptance_environments = {row[2] for row in acceptance_tuples}
    for gate, component_id, environment in acceptance_tuples:
        if gate not in known_gates:
            raise ManifestError(f"acceptance requirement references unknown gate: {gate}")
        if component_id not in component_ids:
            raise ManifestError(
                f"acceptance requirement references unknown component: {component_id}"
            )
        if (gate, component_id) not in provider_gate_pairs:
            raise ManifestError(
                "acceptance requirement gate/component pair is absent from "
                f"source-pinned inventory: {gate} / {component_id}"
            )
    required_profile_pairs = {
        pair for pair in provider_gate_pairs if not pair[0].startswith(("lock:", "rebuild:"))
    }
    mapped_profile_pairs = {(gate, component_id) for gate, component_id, _ in acceptance_tuples}
    if missing_pairs := required_profile_pairs - mapped_profile_pairs:
        raise ManifestError(
            f"acceptance requirements omit provider/gate pairs: {sorted(missing_pairs)[:10]}"
        )
    required_domain_gates = {
        *(
            f"compatibility:{item['id']}"
            for item in candidate["compatibility"]
            if item["evidence_required"]
        ),
        *(
            f"migration:{item['id']}"
            for item in candidate["migrations"]
            if item["evidence_required"]
        ),
        *(f"rollback:{item['id']}" for item in candidate["rollback"] if item["evidence_required"]),
    }
    inventory_gates = {gate for role in inventory_roles for gate in role["required_gates"]}
    if missing := required_domain_gates - inventory_gates:
        raise ManifestError(
            f"source-pinned component inventory omits required lifecycle gates: {sorted(missing)}"
        )
    python_role = next(
        role for role in inventory_roles if role["capability"] == "python_distributions"
    )
    expected_python_gates = {
        gate for lock in python_locks for gate in (f"lock:{lock['id']}", f"rebuild:{lock['id']}")
    }
    actual_python_gates = {
        gate for gate in python_role["required_gates"] if gate.startswith(("lock:", "rebuild:"))
    }
    if actual_python_gates != expected_python_gates:
        raise ManifestError("Python inventory lock/rebuild gates differ from declared locks")

    evidence_by_hash: dict[str, dict[str, Any]] = {}
    artifact_digests_by_component: dict[str, set[str]] = {}
    for artifact in candidate["artifacts"]:
        artifact_digests_by_component.setdefault(artifact["component"], set()).add(
            artifact["file"]["sha256"]
        )
    for evidence in payload["evidence"]:
        if evidence["gate"].startswith(("lock:", "rebuild:")):
            raise ManifestError("Python lock/rebuild gates cannot be satisfied by generic evidence")
        if evidence["gate"] not in known_gates:
            raise ManifestError(f"evidence references unknown gate: {evidence['gate']}")
        if evidence["candidate_id"] != candidate_id or evidence["profile_id"] != profile_id:
            raise ManifestError("evidence candidate/profile identity mismatch")
        if evidence["source_commit"] != candidate["source"]["commit"]:
            raise ManifestError("evidence source commit mismatch")
        if evidence["environment"] not in acceptance_environments:
            raise ManifestError("evidence targets an undeclared acceptance environment")
        component_id = evidence["component_id"]
        if component_id not in component_ids:
            raise ManifestError(f"evidence references unknown component: {component_id}")
        if evidence["artifact_sha256"] not in artifact_digests_by_component.get(
            component_id, set()
        ):
            raise ManifestError(
                f"evidence artifact digest is not bound to component {component_id}"
            )
        if (evidence["gate"], component_id) not in provider_gate_pairs:
            raise ManifestError(
                f"evidence gate/component pair is absent from source-pinned inventory: "
                f"{evidence['gate']} / {component_id}"
            )
        if (evidence["gate"], component_id, evidence["environment"]) not in acceptance_set:
            raise ManifestError("evidence does not match a declared acceptance requirement")
        if evidence["evidence_sha256"] in evidence_by_hash:
            raise ManifestError("duplicate evidence digest")
        _check_identity(evidence, "evidence_sha256", "evidence_sha256")
        evidence_by_hash[evidence["evidence_sha256"]] = evidence

    compatibility_subjects = _compatibility_subjects(
        candidate["compatibility_coverage"], required_compatibility_scopes
    )
    compatibility_evidence: dict[str, str] = {}
    evidence_by_subject: dict[str, list[dict[str, Any]]] = {}
    for evidence in evidence_by_hash.values():
        subject_digest = evidence.get("compatibility_subject_sha256")
        if subject_digest is None:
            continue
        evidence_by_subject.setdefault(subject_digest, []).append(evidence)
    if set(evidence_by_subject) - set(compatibility_subjects):
        raise ManifestError("evidence references an undeclared compatibility subject")
    for subject_digest, (component_id, scope) in compatibility_subjects.items():
        matching = evidence_by_subject.get(subject_digest, [])
        if len(matching) != 1:
            raise ManifestError("confirmed compatibility subject requires exactly one evidence row")
        evidence = matching[0]
        if (
            evidence["component_id"] != component_id
            or evidence["gate"] != compatibility_gates.get(component_id)
            or evidence["outcome"] != "passed"
            or evidence["environment"] != scope["environment_id"]
            or evidence.get("runtime_id") != scope["runtime_id"]
        ):
            raise ManifestError(
                "compatibility subject evidence scope, identity, or gate does not match"
            )
        compatibility_evidence[evidence["evidence_sha256"]] = component_id

    decision = payload["release_decision"]
    if decision["candidate_id"] != candidate_id or decision["profile_id"] != profile_id:
        raise ManifestError("release decision candidate/profile identity mismatch")
    if decision["owner_approval"]["decision"] != decision["decision"]:
        raise ManifestError("owner approval decision is inconsistent")
    if decision["decision"] == "accepted":
        if unresolved_roles:
            raise ManifestError(
                "accepted decision has unresolved required component inventory roles: "
                f"{sorted(unresolved_roles)}"
            )
        if any(
            capability["status"] != "supported" for capability in profile["capabilities"].values()
        ):
            raise ManifestError("accepted decision has a capability not marked supported")
        if any(
            not item["evidence_required"]
            for item in (*candidate["migrations"], *candidate["rollback"])
        ):
            raise ManifestError(
                "accepted decision must require evidence for every migration and rollback"
            )
        required_gates = {
            gate
            for capability in profile["capabilities"].values()
            for gate in capability["required_gates"]
            if not gate.startswith(("lock:", "rebuild:"))
        }
        # Gate identities for evidence_required migrations and rollbacks are
        # `migration:<id>` and `rollback:<id>` respectively.
        required_gates.update(
            f"migration:{item['id']}"
            for item in candidate["migrations"]
            if item["evidence_required"]
        )
        required_gates.update(
            f"rollback:{item['id']}" for item in candidate["rollback"] if item["evidence_required"]
        )
        required_gates.update(
            f"compatibility:{item['id']}"
            for item in candidate["compatibility"]
            if item["evidence_required"]
        )
        refs = set(decision["evidence_refs"])
        if not set(compatibility_evidence) <= refs:
            raise ManifestError("accepted decision omits compatibility relation evidence")
        if not refs or not refs <= (set(evidence_by_hash) | reconstruction_hashes):
            raise ManifestError("accepted decision evidence refs are missing or unresolved")
        if not reconstruction_hashes <= refs:
            raise ManifestError("accepted decision omits Python lock reconstruction receipts")
        selected = [evidence_by_hash[ref] for ref in refs if ref in evidence_by_hash]
        if any(evidence["outcome"] != "passed" for evidence in selected):
            raise ManifestError("accepted decision references non-passing evidence")
        for ref in refs & reconstruction_hashes:
            if not any(
                row["receipt"]["sha256"] == ref
                and reconstructions[(row["lock_id"], row["python_runtime_id"], row["environment"])][
                    "outcome"
                ]
                == "passed"
                for row in payload["lock_reconstructions"]
            ):
                raise ManifestError("accepted decision references non-passing lock reconstruction")
        required_gates.update(required_domain_gates)
        required_tuples = {row for row in acceptance_tuples if row[0] in required_gates}
        passing_tuples = {
            (evidence["gate"], evidence["component_id"], evidence["environment"])
            for evidence in selected
            if evidence["outcome"] == "passed"
        }
        if missing_coverage := required_tuples - passing_tuples:
            preview = sorted(missing_coverage)[:10]
            raise ManifestError(
                f"accepted decision lacks passing gate/component/environment coverage: {preview}"
            )
        raise ManifestError(
            "accepted decision is unavailable: validation has no external trusted inventory "
            "baseline or authenticated evidence/owner-approval attestation; this validator "
            "checks structure only. Trusted acceptance requires a verifier, schema, and "
            "policy invoked outside candidate-controlled code from a protected, independently "
            "pinned identity"
        )
    if policy is _V3_POLICY:
        web_console_facts = _validate_v3_web_console(
            candidate,
            profile,
            components,
            candidate["locks"],
            candidate["artifacts"],
            source,
            bundle,
            tracked,
            source_commit,
            reference_collector,
        )
    _clean_exact_source(source, source_commit)
    references = reference_collector.freeze()
    references_by_role = {item.role: item for item in references}
    candidate_facts = CandidateFacts(
        schema_version=payload["schema_version"],
        manifest_digest=sha256_bytes(canonical_json_bytes(payload)),
        candidate_id=candidate_id,
        profile_id=profile_id,
        candidate_digest=sha256_bytes(canonical_json_bytes(candidate)),
        profile_digest=sha256_bytes(canonical_json_bytes(profile)),
        source_commit=source_commit,
    )
    facts = ValidatedReleaseFacts(
        candidate=candidate_facts,
        policies=PolicyFacts(
            schema_digest,
            component_inventory_digest,
            compatibility_policy_digest,
            profile["acceptance_policy"]["sha256"],
        ),
        components=tuple(
            ComponentFact(
                row["id"],
                row["kind"],
                row["version"],
                references_by_role[f"component:{row['id']}:descriptor"],
                tuple(
                    references_by_role[f"component:{row['id']}:source:{reference['path']}"]
                    for reference in row["source_files"]
                ),
            )
            for row in components
        ),
        verified_references=references,
        lock_requirements=tuple(
            LockRequirementFact(component, ecosystem, purpose, environment, runtime_id)
            for component, ecosystem, purpose, environment, runtime_id in requirement_tuples
        ),
        locks=tuple(
            LockFact(
                row["id"],
                row["ecosystem"],
                row["purpose"],
                row["target_environment"],
                row.get("python_runtime_id"),
                tuple(row["covered_components"]),
                references_by_role[f"lock:{row['id']}"],
            )
            for row in candidate["locks"]
        ),
        acceptance_requirements=tuple(
            AcceptanceRequirementFact(row["gate"], row["component_id"], row["environment"])
            for row in acceptance_rows
        ),
        compatibility_scopes=tuple(
            RuntimeScopeFact(
                component_id, scope["environment_id"], scope["runtime_id"], scope["runtime_status"]
            )
            for component_id, scopes in required_compatibility_scopes.items()
            for scope in scopes
        ),
        compatibility_providers=tuple(
            CompatibilityProviderFact(
                row["component_id"],
                row["status"],
                row["inventory_status"],
                tuple(fmt["format_id"] for fmt in row["formats"]),
            )
            for row in candidate["compatibility_coverage"]
        ),
        compatibility_relations=tuple(
            RelationFact(
                f"{row['component_id']}|{fmt['format_id']}|{relation['predecessor']['release_id']}|"
                f"{relation['scope']['environment_id']}|{relation['scope']['runtime_id'] or ''}",
                row["component_id"],
                relation["scope"]["environment_id"],
                relation["scope"]["runtime_id"],
                fmt["format_id"],
                relation["disposition"],
            )
            for row in candidate["compatibility_coverage"]
            if row["status"] != "unknown-blocking"
            for fmt in row["formats"]
            for relation in fmt["relations"]
        ),
        compatibility_subjects=tuple(
            CompatibilitySubjectFact(
                digest,
                component,
                scope["environment_id"],
                scope["runtime_id"],
                scope["runtime_status"],
            )
            for digest, (component, scope) in compatibility_subjects.items()
        ),
        compatibility_evidence=tuple(compatibility_evidence),
        evidence=tuple(
            EvidenceFact(
                row["evidence_sha256"],
                row["gate"],
                row["component_id"],
                row["environment"],
                row.get("runtime_id"),
                row.get("compatibility_subject_sha256"),
                row["outcome"],
                row["artifact_sha256"],
            )
            for row in evidence_by_hash.values()
        ),
        migrations_rollbacks=tuple(
            MigrationRollbackFact(kind, row["id"], row["evidence_required"])
            for kind, rows in (
                ("migration", candidate["migrations"]),
                ("rollback", candidate["rollback"]),
            )
            for row in rows
        ),
        reconstructions=tuple(
            ReconstructionFact(
                key[0],
                key[1],
                key[2],
                receipt["outcome"],
                next(
                    row["receipt"]["sha256"]
                    for row in payload["lock_reconstructions"]
                    if (row["lock_id"], row["python_runtime_id"], row["environment"]) == key
                ),
            )
            for key, receipt in reconstructions.items()
        ),
        build_contexts=tuple(
            BuildContextFact(
                row["context_sha256"],
                row["component_id"],
                row["python_runtime_id"],
                row["target"]["environment"],
                row["bootstrap_lock_id"],
                row["bootstrap_lock_sha256"],
                row["bootstrap_reconstruction_receipt_sha256"],
            )
            for row in payload.get("build_contexts", [])
        ),
        hooks=tuple(
            HookFact(
                row["attestation_sha256"],
                row["context_sha256"],
                row["operation"],
                row["status"],
                row.get("final_lock_id"),
                row.get("final_lock_sha256"),
                row.get("reconstruction_receipt_sha256"),
            )
            for row in payload.get("hook_attestations", [])
        ),
        builds=tuple(
            BuildFact(
                row["attestation_sha256"],
                row["context_sha256"],
                row["hook_attestation_sha256"],
                row["python_runtime_id"],
                row["target_environment"],
                row["operation"],
                row["final_lock_id"],
                row["final_lock_sha256"],
                row["reconstruction_receipt_sha256"],
                row["outcome"],
                row["artifact"]["sha256"] if row.get("artifact") else None,
            )
            for row in payload.get("build_attestations", [])
        ),
        artifacts=tuple(
            ArtifactFact(
                row["component"],
                row["kind"],
                references_by_role[f"artifact:{row['component']}:{row['file']['path']}"],
            )
            for row in candidate["artifacts"]
        ),
        web_console=web_console_facts,
    )
    from scripts.release_obligation_report import _report_from_validated_facts

    return _report_from_validated_facts(facts)


def _validate_candidate_v2_and_report(
    payload: dict[str, Any], source_root: str | Path, candidate_bundle_root: str | Path
) -> ObligationReport:
    """Preserve v2 checkpoints and errors under the immutable v2 policy."""
    return _validate_candidate_pipeline_and_report(
        payload, source_root, candidate_bundle_root, policy=_V2_POLICY
    )


def _validate_candidate_v3_and_report(
    payload: dict[str, Any], source_root: str | Path, candidate_bundle_root: str | Path
) -> ObligationReport:
    """Run v3 through the shared checked-material pipeline and its web obligations."""
    return _validate_candidate_pipeline_and_report(
        payload, source_root, candidate_bundle_root, policy=_V3_POLICY
    )


def _validate_candidate_and_report(
    payload: dict[str, Any], source_root: str | Path, candidate_bundle_root: str | Path
) -> ObligationReport:
    """Dispatch exact v3 manifests; all other payloads retain the v2 schema path."""
    if (
        isinstance(payload, dict)
        and _policy_for_version(payload.get("schema_version")) is _V3_POLICY
    ):
        return _validate_candidate_v3_and_report(payload, source_root, candidate_bundle_root)
    return _validate_candidate_v2_and_report(payload, source_root, candidate_bundle_root)


def validate_candidate(
    payload: dict[str, Any],
    source_root: str | Path,
    candidate_bundle_root: str | Path,
) -> None:
    """Validate manifest structure, identities, tracked refs, and acceptance gates."""
    _validate_candidate_and_report(payload, source_root, candidate_bundle_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--candidate-bundle-root", type=Path, required=True)
    parser.add_argument("--print-sha256", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = load_manifest(args.manifest)
        validate_candidate(payload, args.source_root, args.candidate_bundle_root)
        if args.print_sha256:
            print(manifest_sha256(payload))
        else:
            print("Manifest contract valid; this is not a release acceptance decision.")
    except ManifestError as exc:
        print(f"manifest validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
