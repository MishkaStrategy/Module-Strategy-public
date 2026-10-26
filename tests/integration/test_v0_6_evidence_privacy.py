from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "integration-ci.yml"
JUNIT_FILES = (
    "core-contracts.xml",
    "cross-layer.xml",
    "position-exit.xml",
    "setup-entry.xml",
    "signal-bot.xml",
    "trading-runner.xml",
)


def _run_sanitizer(
    reports: Path,
    *,
    hostname: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOSTNAME": hostname,
            "RUNNER_NAME": hostname,
        }
    )
    return subprocess.run(
        [sys.executable, str(SANITIZER), str(reports)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_v0_6_junit_hostnames_are_redacted(tmp_path: Path) -> None:
    reports = tmp_path / "v0-6-executable-acceptance"
    reports.mkdir()
    private_hostname = "starter-" + "private-v0-6-runner"
    for filename in JUNIT_FILES:
        (reports / filename).write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites name="pytest tests">'
            '<testsuite name="pytest" errors="0" failures="0" skipped="0" '
            f'tests="1" time="0.001" hostname="{private_hostname}">'
            '<testcase classname="tests.test_example" name="test_example" time="0.001" />'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )

    result = _run_sanitizer(reports, hostname=private_hostname)

    assert result.returncode == 0, result.stderr
    status = json.loads((reports / "privacy-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "PASS"
    assert status["files_scanned"] == len(JUNIT_FILES)
    assert status["credential_findings"] == 0
    assert status["replacements"] == len(JUNIT_FILES)
    for filename in JUNIT_FILES:
        payload = (reports / filename).read_text(encoding="utf-8")
        assert private_hostname not in payload
        assert 'hostname="redacted-runner"' in payload


def test_v0_6_workflow_sanitizes_before_hash_and_release_publication() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    sanitize = text.index("Sanitize and verify exact acceptance evidence privacy")
    hashes = text.index("Record sanitized evidence hashes")
    assemble = text.index("Assemble exact acceptance release evidence")
    publish = text.index("Publish exact v0.6 acceptance evidence")
    diagnostics = text.index("Upload v0.6 acceptance failure diagnostics")

    assert sanitize < hashes < assemble < publish < diagnostics
    assert "python3 scripts/sanitize_ci_evidence.py reports/v0-6-executable-acceptance" in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: reports/v0-6-executable-release" in text
    assert "asset_name: v0-6-executable-acceptance-${{ matrix.python-version }}.tar.gz" in text
