from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from modular_research_runner.futures_evidence import _expected_application_run_id

_CORE_RUN_ID = "core-run-identity-parity"
_FUNDING_IDENTITY = "a" * 64
_MARK_IDENTITY = "b" * 64

_AUTHORITATIVE_ID_SCRIPT = """
import sys

from modular_backtest.funding_application import application_run_id
from modular_backtest.valuation import valuation_application_run_id


class IdentitySource:
    def __init__(self, digest: str) -> None:
        self._digest = digest

    def identity_sha256(self) -> str:
        return self._digest


funding = sys.argv[1] == "1"
mark = sys.argv[2] == "1"
funding_source = IdentitySource("a" * 64) if funding else None
mark_source = IdentitySource("b" * 64) if mark else None
authoritative = application_run_id("core-run-identity-parity", funding_source)
authoritative = valuation_application_run_id(
    authoritative,
    "mark" if mark else "last",
    mark_source,
)
print(authoritative)
"""


def _authoritative_application_run_id(*, funding: bool, mark: bool) -> str:
    repo_root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _AUTHORITATIVE_ID_SCRIPT,
            "1" if funding else "0",
            "1" if mark else "0",
        ],
        cwd=repo_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("funding", "mark"),
    [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_application_identity_matches_exact_candidate_backtest(
    funding: bool,
    mark: bool,
) -> None:
    authoritative = _authoritative_application_run_id(funding=funding, mark=mark)
    research = _expected_application_run_id(
        _CORE_RUN_ID,
        funding_source_identity_sha256=_FUNDING_IDENTITY if funding else None,
        mark_source_identity_sha256=_MARK_IDENTITY if mark else None,
    )

    assert research == authoritative
