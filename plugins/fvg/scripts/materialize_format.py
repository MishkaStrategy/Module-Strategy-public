from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path.cwd().resolve()
PREVIEW_PATH = ROOT / "fvg-format-preview.json"
PREVIEW_PATCH_PATH = ROOT / "fvg-format-preview.patch"
FVG_PATHS = (
    "plugins/fvg/src",
    "plugins/fvg/tests",
    "plugins/fvg/benchmarks",
    "plugins/fvg/scripts",
)


def run(command: list[str]) -> None:
    process = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        check=False,
    )
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


def write_preview(*, format_clean: bool) -> None:
    payload = {
        "schema_version": 3,
        "ruff_version": "0.12.0",
        "mode": "check-only",
        "format_clean": format_clean,
    }
    PREVIEW_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    format_process = subprocess.run(
        [
            "uvx",
            "--from",
            "ruff==0.12.0",
            "ruff",
            "format",
            "--diff",
            *FVG_PATHS,
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    PREVIEW_PATCH_PATH.write_text(format_process.stdout, encoding="utf-8")
    write_preview(format_clean=format_process.returncode == 0)
    if format_process.returncode:
        sys.stdout.write(format_process.stdout)
        return format_process.returncode

    run(
        [
            "uvx",
            "--from",
            "ruff==0.12.0",
            "ruff",
            "check",
            *FVG_PATHS,
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
