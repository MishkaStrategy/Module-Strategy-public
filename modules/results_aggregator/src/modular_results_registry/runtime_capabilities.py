from __future__ import annotations

import inspect
import os

from .errors import UnsupportedPlatformError

_REQUIRED_DIR_FD_FUNCTIONS = (
    "open",
    "stat",
    "mkdir",
    "unlink",
    "link",
)
_REQUIRED_REPLACE_PARAMETERS = frozenset({"src_dir_fd", "dst_dir_fd"})


def _replace_supports_directory_descriptors() -> bool:
    replace = getattr(os, "replace", None)
    if replace is None:
        return False
    try:
        parameters = inspect.signature(replace).parameters
    except (TypeError, ValueError):
        return False
    return _REQUIRED_REPLACE_PARAMETERS.issubset(parameters)


def missing_posix_capabilities() -> tuple[str, ...]:
    missing: list[str] = []
    if os.name != "posix":
        missing.append("os.name=posix")
    for attribute in ("O_NOFOLLOW", "fchmod", "fsync"):
        if not hasattr(os, attribute):
            missing.append(f"os.{attribute}")
    supports_dir_fd: set[object] = set(getattr(os, "supports_dir_fd", ()))
    for name in _REQUIRED_DIR_FD_FUNCTIONS:
        function = getattr(os, name, None)
        if function is None or function not in supports_dir_fd:
            missing.append(f"os.{name}(dir_fd=...)")
    if not _replace_supports_directory_descriptors():
        missing.append("os.replace(src_dir_fd=..., dst_dir_fd=...)")
    return tuple(missing)


def require_posix_storage_capabilities() -> None:
    missing = missing_posix_capabilities()
    if missing:
        raise UnsupportedPlatformError(
            "results registry SQLite/artifact backend requires audited POSIX "
            "no-follow and dir-fd filesystem semantics; missing: " + ", ".join(missing)
        )
