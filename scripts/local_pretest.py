#!/usr/bin/env python3
"""Local computer pre-test entry point."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pretest_support.app import main, write_report as _write_report  # noqa: E402
from pretest_support.configs import write_configs as _write_configs  # noqa: E402
from pretest_support.dataset import (  # noqa: E402
    inspect_data as _inspect_data,
    locate_data_file as _locate_data_file,
    read_summary as _read_summary,
)
from pretest_support.models import (  # noqa: E402
    DataFacts,
    PretestError,
    Reporter,
)
from pretest_support.results import reset_generated_results as _reset_generated_results  # noqa: E402

__all__ = [
    "DataFacts",
    "PretestError",
    "Reporter",
    "_inspect_data",
    "_locate_data_file",
    "_read_summary",
    "_reset_generated_results",
    "_write_configs",
    "_write_report",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
