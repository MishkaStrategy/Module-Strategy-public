#!/usr/bin/env python3
"""Run pytest collection and write exact node IDs independently of terminal verbosity."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import pytest


class CollectionRecorder:
    """Capture collected items and collection failures through pytest hooks."""

    def __init__(self) -> None:
        self.session: pytest.Session | None = None
        self.collection_errors: list[dict[str, str]] = []

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.session = session

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors.append(
                {
                    "nodeid": report.nodeid,
                    "longrepr": str(report.longrepr),
                }
            )

    def payload(self, exit_code: int) -> dict[str, Any]:
        items = () if self.session is None else self.session.items
        node_ids = list(dict.fromkeys(item.nodeid for item in items))
        return {
            "schema_version": 1,
            "exit_code": exit_code,
            "collected": len(node_ids),
            "node_ids": node_ids,
            "collection_errors": self.collection_errors,
        }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def split_pytest_args(argv: Sequence[str] | None = None) -> tuple[Path, list[str]]:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        raise ValueError("separate pytest arguments with --")
    separator = raw.index("--")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(raw[:separator])
    pytest_args = raw[separator + 1 :]
    if not pytest_args:
        raise ValueError("at least one pytest argument is required after --")
    return args.output, pytest_args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        output, pytest_args = split_pytest_args(argv)
    except ValueError as exc:
        print(f"pytest collection recorder error: {exc}", file=sys.stderr)
        return 2

    recorder = CollectionRecorder()
    exit_code = int(pytest.main(pytest_args, plugins=[recorder]))
    _atomic_write_json(output, recorder.payload(exit_code))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
