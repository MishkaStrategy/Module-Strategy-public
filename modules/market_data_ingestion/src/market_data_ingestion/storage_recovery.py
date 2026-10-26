from __future__ import annotations

import errno
import os
import time
from pathlib import Path, PurePosixPath

from .errors import StorageError
from .registry import SQLiteDatasetRegistry
from .storage import LocalStorage

_DEFAULT_ORPHAN_AGE_SECONDS = 3600.0


def _is_atomic_temporary_name(name: str) -> bool:
    return name.startswith(".") and name.endswith(".tmp") and name.count(".") >= 3


class RecoverableLocalStorage(LocalStorage):
    """LocalStorage entrypoint with deterministic inventory and orphan recovery."""

    def list_manifests(self) -> tuple[str, ...]:
        """Return fail-closed, deterministic manifest paths relative to the storage root."""
        manifest_root = self.root / "manifests"
        manifests: list[str] = []
        for directory_text, directories, files in os.walk(
            manifest_root,
            topdown=True,
            followlinks=False,
        ):
            directory = Path(directory_text)
            directories.sort()
            files.sort()
            for name in directories:
                child = directory / name
                if child.is_symlink():
                    raise StorageError(
                        "SYMLINK_PATH",
                        "manifest inventory contains a symbolic-link directory",
                        False,
                        "storage_recovery",
                        {"path": str(child)},
                    )
            for name in files:
                path = directory / name
                if path.is_symlink():
                    raise StorageError(
                        "SYMLINK_PATH",
                        "manifest inventory contains a symbolic-link file",
                        False,
                        "storage_recovery",
                        {"path": str(path)},
                    )
                if path.suffix.lower() != ".json" or not path.is_file():
                    continue
                safe = self._under_root(path)
                manifests.append(safe.relative_to(self.root).as_posix())
        return tuple(sorted(manifests))

    def find_unregistered_manifests(
        self,
        registry: SQLiteDatasetRegistry,
    ) -> tuple[str, ...]:
        """Return immutable manifests that are not visible through the Registry."""
        if not isinstance(registry, SQLiteDatasetRegistry):
            raise TypeError("registry must be SQLiteDatasetRegistry")
        orphans: list[str] = []
        for relative in self.list_manifests():
            parts = PurePosixPath(relative).parts
            if len(parts) != 3 or parts[0] != "manifests":
                raise StorageError(
                    "MANIFEST_PATH_INVALID",
                    "manifest inventory contains an unexpected path layout",
                    False,
                    "storage_recovery",
                    {"path": relative},
                )
            dataset_id = parts[1]
            filename = parts[2]
            if not filename.startswith("v") or not filename.endswith(".json"):
                raise StorageError(
                    "MANIFEST_PATH_INVALID",
                    "manifest inventory contains an invalid version filename",
                    False,
                    "storage_recovery",
                    {"path": relative},
                )
            try:
                version = int(filename[1:-5])
            except ValueError as exc:
                raise StorageError(
                    "MANIFEST_PATH_INVALID",
                    "manifest inventory contains a non-numeric version",
                    False,
                    "storage_recovery",
                    {"path": relative},
                ) from exc
            if version <= 0:
                raise StorageError(
                    "MANIFEST_PATH_INVALID",
                    "manifest inventory contains a non-positive version",
                    False,
                    "storage_recovery",
                    {"path": relative},
                )
            if registry.inspect(dataset_id, version) is None:
                orphans.append(relative)
        return tuple(orphans)

    def cleanup_orphan_temporary_files(
        self,
        *,
        max_age_seconds: float = _DEFAULT_ORPHAN_AGE_SECONDS,
    ) -> tuple[Path, ...]:
        """Remove only aged temporary files created by LocalStorage atomic publication."""
        if isinstance(max_age_seconds, bool) or max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        cutoff = time.time() - float(max_age_seconds)
        candidates: list[Path] = []
        for directory_text, directories, files in os.walk(
            self.root,
            topdown=True,
            followlinks=False,
        ):
            directory = Path(directory_text)
            directories.sort()
            files.sort()
            for name in directories:
                child = directory / name
                if child.is_symlink():
                    raise StorageError(
                        "SYMLINK_PATH",
                        "storage recovery encountered a symbolic-link directory",
                        False,
                        "storage_recovery",
                        {"path": str(child)},
                    )
            for name in files:
                if not _is_atomic_temporary_name(name):
                    continue
                path = directory / name
                if path.is_symlink():
                    raise StorageError(
                        "SYMLINK_PATH",
                        "storage recovery encountered a symbolic-link temporary file",
                        False,
                        "storage_recovery",
                        {"path": str(path)},
                    )
                try:
                    metadata = path.stat()
                except FileNotFoundError:
                    continue
                if not path.is_file() or metadata.st_mtime > cutoff:
                    continue
                candidates.append(self._under_root(path))

        removed: list[Path] = []
        synced_directories: set[Path] = set()
        for path in sorted(candidates, key=lambda item: item.as_posix()):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise self._filesystem_error(
                    exc,
                    operation="cleaning an orphan atomic temporary file",
                    path=path,
                ) from exc
            removed.append(path)
            synced_directories.add(path.parent)

        for directory in sorted(synced_directories, key=lambda item: item.as_posix()):
            self._fsync_directory(directory)
        return tuple(removed)

    def _fsync_directory(self, directory: Path) -> None:
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                return
            raise self._filesystem_error(
                exc,
                operation="syncing an orphan-cleanup directory",
                path=directory,
            ) from exc


def open_recoverable_storage(root: Path) -> RecoverableLocalStorage:
    """Open the recovery-capable LocalStorage contract used by Wave 2 consumers."""
    return RecoverableLocalStorage(root)
