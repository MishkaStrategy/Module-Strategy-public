from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"


def _run_sanitizer(
    evidence: Path,
    *,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SANITIZER), str(evidence)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_research_junit_hostname_is_redacted(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    private_hostname = "private-" + "research-runner.local"
    junit = reports / "research-runner-tests.xml"
    junit.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        '<testsuite name="pytest" errors="0" failures="0" skipped="0" '
        f'tests="1" time="0.001" hostname="{private_hostname}">'
        '<testcase classname="tests.test_example" name="test_example" time="0.001" />'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    result = _run_sanitizer(
        reports,
        extra_env={
            "RUNNER_NAME": private_hostname,
            "HOSTNAME": private_hostname,
        },
    )

    assert result.returncode == 0, result.stderr
    payload = junit.read_text(encoding="utf-8")
    status = json.loads((reports / "privacy-status.json").read_text(encoding="utf-8"))
    assert private_hostname not in payload
    assert 'hostname="redacted-runner"' in payload
    assert status["status"] == "PASS"
    assert status["credential_findings"] == 0
    assert status["replacements"] > 0
