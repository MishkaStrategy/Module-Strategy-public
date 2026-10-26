#!/usr/bin/env python3
"""Reset allowlisted result roots owned by the local pretest suite."""

from __future__ import annotations

import argparse
from pathlib import Path

from pretest_support.models import PretestError
from pretest_support.results import reset_generated_results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("roots", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        removed = reset_generated_results(args.workspace, args.roots)
    except PretestError as exc:
        raise SystemExit(f"error: {exc}") from exc
    removed_names = {path.name for path in removed}
    for name in args.roots:
        state = "removed" if name in removed_names else "absent"
        print(f"[INFO] generated result root {name}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
