from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import TextIO

from .errors import IntegrityError, ResourceLimitError, ValidationError

_TEMP_ATTEMPTS = 32
_ALLOWED_MODE_BITS = 0o777


def path_lexists(path: str | Path) -> bool:
    return os.path.lexists(Path(path))


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
        raise IntegrityError("output parent path is invalid")
    try:
        descriptor = os.open(parts[0], _directory_open_flags())
    except OSError as exc:
        raise IntegrityError("output parent cannot be opened safely") from exc
    try:
        for component in parts[1:]:
            try:
                next_descriptor = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise IntegrityError("output parent cannot be opened safely")
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(component, _directory_open_flags(), dir_fd=descriptor)
                except OSError as exc:
                    raise IntegrityError("output parent cannot be created safely") from exc
            except OSError as exc:
                raise IntegrityError("output parent cannot be opened safely") from exc
            info = os.fstat(next_descriptor)
            if not stat.S_ISDIR(info.st_mode):
                os.close(next_descriptor)
                raise IntegrityError("output parent must be a directory")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _target_info(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntegrityError("output target cannot be inspected") from exc


def _validate_target_info(info: os.stat_result | None) -> None:
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode):
        raise IntegrityError("output target cannot be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise IntegrityError("output target must be a regular file")


def ensure_safe_parent(path: str | Path) -> Path:
    """Create and validate every parent component without following symlinks."""
    target = Path(path).absolute()
    _validate_target_info(_target_info(target))
    descriptor = _open_directory_chain_nofollow(target.parent, create=True)
    os.close(descriptor)
    _validate_target_info(_target_info(target))
    return target


def _open_parent_directory(target: Path) -> int:
    return _open_directory_chain_nofollow(target.parent)


def _stat_target_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntegrityError("output target cannot be inspected") from exc


def _validate_existing_target_at(parent_fd: int, name: str) -> os.stat_result | None:
    info = _stat_target_at(parent_fd, name)
    _validate_target_info(info)
    return info


def _validate_mode(mode: int) -> None:
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise ValidationError("output mode must be an integer")
    if mode < 0 or mode & ~_ALLOWED_MODE_BITS:
        raise ValidationError("output mode contains unsupported bits")


def _unlink_if_present(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _rollback_new_target(parent_fd: int, target_name: str) -> None:
    try:
        os.unlink(target_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise IntegrityError("output publication failed and rollback was incomplete") from exc


def _rollback_replacement(parent_fd: int, target_name: str, backup_name: str) -> None:
    try:
        os.replace(
            backup_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise IntegrityError("output publication failed and rollback was incomplete") from exc


def atomic_write_bytes(
    path: str | Path,
    data: bytes,
    *,
    mode: int = 0o600,
    replace_existing: bool = True,
    max_bytes: int | None = None,
) -> Path:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    _validate_mode(mode)
    if max_bytes is not None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if len(data) > max_bytes:
            raise ResourceLimitError("output exceeds configured byte limit")
    if not isinstance(replace_existing, bool):
        raise ValidationError("replace_existing must be a boolean")

    target = Path(path).absolute()
    initial_exists = os.path.lexists(target)
    target = ensure_safe_parent(target)
    initial_info = _target_info(target) if initial_exists else None
    _validate_target_info(initial_info)
    if initial_exists and not replace_existing:
        raise IntegrityError("output target already exists")

    parent_fd = _open_parent_directory(target)
    temporary_name: str | None = None
    backup_name: str | None = None
    published_new = False
    replaced_existing = False
    temporary_fd = -1
    try:
        current = _validate_existing_target_at(parent_fd, target.name)
        if initial_exists:
            if current is None or initial_info is None:
                raise IntegrityError("output target changed during preflight")
            if (current.st_dev, current.st_ino) != (
                initial_info.st_dev,
                initial_info.st_ino,
            ):
                raise IntegrityError("output target changed during preflight")
        elif current is not None:
            raise IntegrityError("output target already exists")

        for _ in range(_TEMP_ATTEMPTS):
            candidate = f".{target.name}.{secrets.token_hex(12)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                temporary_fd = os.open(candidate, flags, mode, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise IntegrityError("output temporary file cannot be opened safely") from exc
            temporary_name = candidate
            break
        else:
            raise IntegrityError("cannot allocate a unique output temporary file")

        os.fchmod(temporary_fd, mode)
        descriptor = temporary_fd
        temporary_fd = -1
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        current = _validate_existing_target_at(parent_fd, target.name)
        if initial_exists:
            if initial_info is None:
                raise IntegrityError("output target changed during preflight")
            if current is None or (current.st_dev, current.st_ino) != (
                initial_info.st_dev,
                initial_info.st_ino,
            ):
                raise IntegrityError("output target changed during preflight")
            for _ in range(_TEMP_ATTEMPTS):
                candidate = f".{target.name}.{secrets.token_hex(12)}.bak"
                try:
                    os.link(
                        target.name,
                        candidate,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise IntegrityError("output backup cannot be created safely") from exc
                backup_name = candidate
                break
            else:
                raise IntegrityError("cannot allocate a unique output backup file")
            os.fsync(parent_fd)
            try:
                os.replace(
                    temporary_name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                temporary_name = None
                replaced_existing = True
                os.fsync(parent_fd)
            except BaseException as exc:
                if replaced_existing and backup_name is not None:
                    _rollback_replacement(parent_fd, target.name, backup_name)
                    backup_name = None
                if isinstance(exc, IntegrityError):
                    raise
                raise IntegrityError("output publication failed") from exc
            _unlink_if_present(parent_fd, backup_name)
            backup_name = None
            os.fsync(parent_fd)
        else:
            if current is not None:
                raise IntegrityError("output target already exists")
            try:
                os.link(
                    temporary_name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                published_new = True
                os.fsync(parent_fd)
                os.unlink(temporary_name, dir_fd=parent_fd)
                temporary_name = None
                os.fsync(parent_fd)
            except BaseException as exc:
                if published_new:
                    _rollback_new_target(parent_fd, target.name)
                    published_new = False
                if isinstance(exc, IntegrityError):
                    raise
                if isinstance(exc, FileExistsError):
                    raise IntegrityError("output target already exists") from exc
                raise IntegrityError("output publication failed") from exc
        return target
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            _unlink_if_present(parent_fd, temporary_name)
        if backup_name is not None:
            _unlink_if_present(parent_fd, backup_name)
        os.close(parent_fd)


def atomic_write_text(
    path: str | Path,
    data: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
    replace_existing: bool = True,
    max_bytes: int | None = None,
) -> Path:
    if not isinstance(data, str):
        raise TypeError("data must be str")
    if encoding.lower().replace("_", "-") != "utf-8":
        raise ValidationError("atomic text output supports UTF-8 only")
    return atomic_write_bytes(
        path,
        data.encode("utf-8"),
        mode=mode,
        replace_existing=replace_existing,
        max_bytes=max_bytes,
    )


def open_append_text_nofollow(path: str | Path, *, mode: int = 0o600) -> TextIO:
    _validate_mode(mode)
    target = ensure_safe_parent(path)
    parent_fd = _open_parent_directory(target)
    try:
        _validate_existing_target_at(parent_fd, target.name)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target.name, flags, mode, dir_fd=parent_fd)
        except OSError as exc:
            raise IntegrityError("append target cannot be opened safely") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise IntegrityError("append target must be a regular file")
            os.fchmod(descriptor, mode)
            return os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_fd)
