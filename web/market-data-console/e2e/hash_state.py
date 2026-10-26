from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path


def manifest(root: Path) -> tuple[str, ...]:
    resolved = root.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("state root must be an existing directory")
    rows: list[str] = []
    with os.scandir(resolved) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name in {".", ".."} or "/" in entry.name or "\x00" in entry.name:
                raise ValueError("state entry name is invalid")
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ValueError(f"state entry is not a regular file: {entry.name}")
            digest = hashlib.sha256()
            with open(entry.path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            rows.append(f"{digest.hexdigest()}  {entry.name}")
    return tuple(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    for row in manifest(args.root):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
