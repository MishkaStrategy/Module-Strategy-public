from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys
import unicodedata

EXCLUDED_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}


def path_key(path: Path) -> str:
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def should_skip(relative: Path) -> bool:
    return any(part in EXCLUDED_NAMES or part.startswith(".venv") for part in relative.parts)


def find_collisions(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if should_skip(relative):
            continue
        grouped[path_key(relative)].append(relative.as_posix())
    return tuple(
        (key, tuple(values)) for key, values in sorted(grouped.items()) if len(set(values)) > 1
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    collisions = find_collisions(root)
    if collisions:
        for key, paths in collisions:
            print(f"collision_key={key}")
            for path in paths:
                print(f"  {path}")
        return 1
    print("path_collision_check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
