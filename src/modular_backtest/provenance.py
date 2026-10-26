from __future__ import annotations

import hashlib
import json
import os
import string
import subprocess
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import ModuleType

import modular_core

from ._version import APPLICATION_VERSION

_SOURCE_SUFFIXES = {".py", ".pyi"}
_HEXADECIMAL = frozenset(string.hexdigits)
_DISTRIBUTION_NAME = "modular-strategy-core"


class ProvenanceError(ValueError):
    """Raised when source provenance cannot be represented safely."""


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    application_version: str
    distribution_version: str | None
    application_source_sha256: str
    core_source_sha256: str
    application_identity_sha256: str
    source_commit_sha: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _module_root(module: ModuleType, name: str) -> Path:
    location = module.__file__
    if location is None:
        raise ProvenanceError(f"cannot resolve source root for {name}")
    root = Path(location).resolve().parent
    if not root.is_dir():
        raise ProvenanceError(f"source root does not exist for {name}: {root}")
    return root


def source_tree_sha256(root: Path, package_name: str) -> str:
    if not isinstance(root, Path) or not root.is_dir():
        raise ProvenanceError(f"source root must be an existing directory: {root}")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ProvenanceError("package_name must be non-empty")

    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (path.suffix in _SOURCE_SUFFIXES or path.name == "py.typed")
    )
    if not files:
        raise ProvenanceError(f"source root contains no package source files: {root}")

    digest = hashlib.sha256()
    package_bytes = package_name.encode("utf-8")
    digest.update(len(package_bytes).to_bytes(4, "big"))
    digest.update(package_bytes)
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _normalized_commit_sha(value: str, source: str) -> str:
    candidate = value.strip().lower()
    if len(candidate) != 40 or any(character not in _HEXADECIMAL for character in candidate):
        raise ProvenanceError(f"{source} must contain exactly 40 hexadecimal characters")
    return candidate


def resolve_source_commit_sha(application_root: Path | None = None) -> str | None:
    explicit = os.environ.get("MODULAR_STRATEGY_SOURCE_SHA")
    if explicit is not None:
        return _normalized_commit_sha(explicit, "MODULAR_STRATEGY_SOURCE_SHA")

    root = application_root or Path(__file__).resolve().parent
    candidates = (Path.cwd(), root.parent.parent)
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            completed = subprocess.run(
                ["git", "-C", str(resolved), "rev-parse", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if completed.returncode == 0:
            return _normalized_commit_sha(completed.stdout, "git HEAD")
    return None


def _distribution_version() -> str | None:
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return None


def build_source_provenance() -> SourceProvenance:
    application_root = Path(__file__).resolve().parent
    core_root = _module_root(modular_core, "modular_core")
    application_source_sha256 = source_tree_sha256(application_root, "modular_backtest")
    core_source_sha256 = source_tree_sha256(core_root, "modular_core")
    distribution_version = _distribution_version()
    identity_payload = {
        "application_version": APPLICATION_VERSION,
        "distribution_version": distribution_version,
        "application_source_sha256": application_source_sha256,
        "core_source_sha256": core_source_sha256,
    }
    application_identity_sha256 = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SourceProvenance(
        application_version=APPLICATION_VERSION,
        distribution_version=distribution_version,
        application_source_sha256=application_source_sha256,
        core_source_sha256=core_source_sha256,
        application_identity_sha256=application_identity_sha256,
        source_commit_sha=resolve_source_commit_sha(application_root),
    )
