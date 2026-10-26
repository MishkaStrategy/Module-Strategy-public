from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import IntegrityError, ResourceLimitError, ValidationError
from .models import ArtifactRecord


@dataclass(frozen=True, slots=True)
class FileLimits:
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    chunk_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_file_bytes, bool)
            or not isinstance(self.max_file_bytes, int)
            or self.max_file_bytes < 1
        ):
            raise ValueError("max_file_bytes must be a positive integer")
        if (
            isinstance(self.chunk_bytes, bool)
            or not isinstance(self.chunk_bytes, int)
            or (self.chunk_bytes < 4096 or self.chunk_bytes > 16 * 1024 * 1024)
        ):
            raise ValueError("chunk_bytes must be an integer between 4 KiB and 16 MiB")


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    path: Path
    size_bytes: int
    sha256: str


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_directory_chain_nofollow(path: Path, *, create: bool = False) -> int:
    absolute = path.absolute()
    parts = absolute.parts
    if not parts:
        raise IntegrityError("directory path is invalid")
    try:
        descriptor = os.open(parts[0], _directory_open_flags())
    except OSError as exc:
        raise IntegrityError("directory root cannot be opened safely") from exc
    try:
        for component in parts[1:]:
            try:
                next_descriptor = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise IntegrityError("artifact parent directory does not exist")
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(component, _directory_open_flags(), dir_fd=descriptor)
                except OSError as exc:
                    raise IntegrityError(
                        "artifact parent directory cannot be created safely"
                    ) from exc
            except OSError as exc:
                raise IntegrityError(
                    "artifact path contains an unsafe directory component"
                ) from exc
            info = os.fstat(next_descriptor)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_descriptor)
                raise IntegrityError("artifact parent component is not a directory")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    parent_fd = _open_directory_chain_nofollow(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            link_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise IntegrityError(f"artifact is not accessible: {path}") from exc
        if stat.S_ISLNK(link_info.st_mode):
            raise IntegrityError("symlink artifacts are forbidden")
        if not stat.S_ISREG(link_info.st_mode):
            raise IntegrityError("artifact must be a regular file")
        try:
            descriptor = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise IntegrityError(f"cannot open artifact safely: {path}") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise IntegrityError("artifact must be a regular file")
            if (link_info.st_dev, link_info.st_ino) != (info.st_dev, info.st_ino):
                raise IntegrityError("artifact changed while being opened")
            return descriptor, info
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_fd)


def _verify_and_read(
    path: str | Path,
    *,
    expected_sha256: str | None,
    expected_size: int | None,
    limits: FileLimits,
    collect_bytes: bool,
) -> tuple[VerifiedFile, bytes | None]:
    if not isinstance(limits, FileLimits):
        raise ValidationError("limits must be FileLimits")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValidationError("expected_sha256 must be a lowercase SHA-256 digest")
    if expected_size is not None and (
        isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0
    ):
        raise ValidationError("expected_size must be a non-negative integer")
    if not isinstance(collect_bytes, bool):
        raise ValidationError("collect_bytes must be boolean")
    source = Path(path)
    descriptor, before = _open_regular_nofollow(source)
    try:
        if before.st_size > limits.max_file_bytes:
            raise ResourceLimitError("artifact exceeds max_file_bytes")
        if expected_size is not None and before.st_size != expected_size:
            raise IntegrityError("artifact size mismatch")
        digest = hashlib.sha256()
        total = 0
        chunks: list[bytes] | None = [] if collect_bytes else None
        while True:
            chunk = os.read(descriptor, limits.chunk_bytes)
            if not chunk:
                break
            total += len(chunk)
            if total > limits.max_file_bytes:
                raise ResourceLimitError("artifact exceeds max_file_bytes while reading")
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable or total != before.st_size:
            raise IntegrityError("artifact changed during verification")
        actual = digest.hexdigest()
        if expected_sha256 is not None and actual != expected_sha256:
            raise IntegrityError("artifact SHA-256 mismatch")
        data = None if chunks is None else b"".join(chunks)
        return VerifiedFile(source, total, actual), data
    finally:
        os.close(descriptor)


def verify_file(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    limits: FileLimits = FileLimits(),
) -> VerifiedFile:
    verified, _ = _verify_and_read(
        path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        limits=limits,
        collect_bytes=False,
    )
    return verified


def read_verified_bytes(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    limits: FileLimits = FileLimits(),
) -> tuple[VerifiedFile, bytes]:
    """Read a bounded regular file from the same descriptor used for verification."""
    verified, data = _verify_and_read(
        path,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        limits=limits,
        collect_bytes=True,
    )
    if data is None:  # pragma: no cover - collect_bytes=True is invariant
        raise IntegrityError("verified byte collection failed")
    return verified, data


class ImmutableArtifactStore:
    """Content-addressed local store that never executes or rewrites artifacts."""

    def __init__(self, root: str | Path, *, limits: FileLimits = FileLimits()) -> None:
        if not isinstance(limits, FileLimits):
            raise ValidationError("limits must be FileLimits")
        root_path = Path(root)
        if root_path.is_symlink():
            raise ValidationError("artifact store root must not be a symlink")
        self.root = root_path.absolute()
        try:
            descriptor = _open_directory_chain_nofollow(self.root, create=True)
        except IntegrityError as exc:
            raise ValidationError("artifact store root must be a safe real directory path") from exc
        os.close(descriptor)
        self.limits = limits

    def target_for_hash(self, sha256: str) -> Path:
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdef" for char in sha256)
        ):
            raise ValidationError("invalid SHA-256")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def import_artifact(self, source: str | Path, record: ArtifactRecord) -> tuple[Path, str]:
        if not isinstance(record, ArtifactRecord):
            raise ValidationError("record must be an ArtifactRecord")
        verified = verify_file(
            source,
            expected_sha256=record.sha256,
            expected_size=record.size_bytes,
            limits=self.limits,
        )
        target = self.target_for_hash(verified.sha256)
        parent_fd = _open_directory_chain_nofollow(target.parent, create=True)
        source_fd, before = _open_regular_nofollow(verified.path)
        temporary_name: str | None = None
        try:
            try:
                existing_info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise IntegrityError("artifact store target cannot be inspected") from exc
            else:
                if stat.S_ISLNK(existing_info.st_mode):
                    raise IntegrityError("artifact store target cannot be a symlink")
                existing = verify_file(
                    target,
                    expected_sha256=record.sha256,
                    expected_size=record.size_bytes,
                    limits=self.limits,
                )
                return existing.path, "DUPLICATE"

            for _ in range(32):
                candidate = f".{target.name}.{os.urandom(12).hex()}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    temporary_fd = os.open(candidate, flags, 0o400, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            else:
                raise IntegrityError("cannot allocate artifact temporary file")

            digest = hashlib.sha256()
            total = 0
            descriptor = temporary_fd
            temporary_fd = -1
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    while True:
                        chunk = os.read(source_fd, self.limits.chunk_bytes)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.limits.max_file_bytes:
                            raise ResourceLimitError(
                                "artifact exceeds max_file_bytes while copying"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fchmod(handle.fileno(), 0o444)
            finally:
                if temporary_fd >= 0:
                    os.close(temporary_fd)

            after = os.fstat(source_fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise IntegrityError("source artifact changed during copy")
            if total != record.size_bytes or digest.hexdigest() != record.sha256:
                raise IntegrityError("copied artifact does not match manifest")
            published = False
            try:
                os.link(
                    temporary_name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                published = True
                os.fsync(parent_fd)
                os.unlink(temporary_name, dir_fd=parent_fd)
                temporary_name = None
                os.fsync(parent_fd)
            except FileExistsError:
                if temporary_name is None:
                    raise IntegrityError("artifact temporary path is missing")
                os.unlink(temporary_name, dir_fd=parent_fd)
                temporary_name = None
                verify_file(
                    target,
                    expected_sha256=record.sha256,
                    expected_size=record.size_bytes,
                    limits=self.limits,
                )
                return target, "DUPLICATE"
            except OSError as exc:
                if published:
                    try:
                        os.unlink(target.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError as rollback_exc:
                        raise IntegrityError(
                            "artifact publication failed and rollback was incomplete"
                        ) from rollback_exc
                raise IntegrityError("artifact store publication failed") from exc
            return target, "IMPORTED"
        finally:
            os.close(source_fd)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def verify_inventory(self) -> tuple[VerifiedFile, ...]:
        root_fd = _open_directory_chain_nofollow(self.root)
        os.close(root_fd)
        results: list[VerifiedFile] = []
        for directory, directory_names, file_names in os.walk(
            self.root, topdown=True, followlinks=False
        ):
            base = Path(directory)
            for name in tuple(directory_names):
                candidate = base / name
                if stat.S_ISLNK(candidate.lstat().st_mode):
                    raise IntegrityError(f"symlink directory found in artifact store: {candidate}")
            for name in sorted(file_names):
                path = base / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise IntegrityError(f"symlink file found in artifact store: {path}")
                if not stat.S_ISREG(info.st_mode):
                    raise IntegrityError(f"non-regular file found in artifact store: {path}")
                expected = path.name
                if len(expected) != 64 or any(
                    character not in "0123456789abcdef" for character in expected
                ):
                    raise IntegrityError(f"invalid content-addressed artifact name: {path}")
                relative = path.relative_to(self.root)
                if relative.parts != (expected[:2], expected[2:4], expected):
                    raise IntegrityError(
                        f"artifact is stored outside its content-addressed path: {path}"
                    )
                results.append(verify_file(path, expected_sha256=expected, limits=self.limits))
        return tuple(sorted(results, key=lambda item: item.path.as_posix()))
