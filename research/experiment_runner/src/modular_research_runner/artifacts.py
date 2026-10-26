from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes


class ArtifactError(RuntimeError):
    """Raised when a local research artifact cannot be committed safely."""


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    relative_path: str
    sha256: str
    size_bytes: int


class AtomicJsonArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        if self.root.is_symlink():
            raise ArtifactError("artifact root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArtifactError("artifact root must be a directory")

    def _target(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ArtifactError("artifact path must be a safe relative path")
        target = self.root.joinpath(*path.parts)
        current = self.root
        for part in path.parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ArtifactError("artifact parent must not be a symlink")
            current.mkdir(exist_ok=True)
        if target.is_symlink():
            raise ArtifactError("artifact target must not be a symlink")
        return target

    def write_json(self, relative_path: str, value: Any) -> ArtifactReference:
        target = self._target(relative_path)
        payload = canonical_json_bytes(value)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        digest = hashlib.sha256(payload).hexdigest()
        return ArtifactReference(
            relative_path=relative_path,
            sha256=digest,
            size_bytes=len(payload),
        )

    def read_json(self, reference: ArtifactReference) -> Any:
        target = self._target(reference.relative_path)
        if not target.is_file():
            raise ArtifactError("artifact does not exist")
        payload = target.read_bytes()
        if len(payload) != reference.size_bytes:
            raise ArtifactError("artifact size does not match its reference")
        if hashlib.sha256(payload).hexdigest() != reference.sha256:
            raise ArtifactError("artifact checksum does not match its reference")
        return json.loads(payload)

    def reference(self, relative_path: str) -> ArtifactReference:
        """Resolve one fixed owner artifact and bind its current immutable bytes."""

        target = self._target(relative_path)
        if not target.is_file():
            raise ArtifactError("artifact does not exist")
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise ArtifactError("artifact cannot be read") from exc
        return ArtifactReference(
            relative_path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
