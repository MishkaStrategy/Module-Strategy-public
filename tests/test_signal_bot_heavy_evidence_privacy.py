from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "signal-bot-heavy-audit.yml"


def test_signal_bot_junit_hostname_is_redacted(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private_runner = "private-signal-bot-runner"
    private_home = "/Users/private-signal-bot-user"

    for name in ("full-tests.xml", "tests.xml"):
        (evidence / name).write_text(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites><testsuite tests="1" failures="0" errors="0" '
            f'hostname="{private_runner}"><testcase name="test_ok" />'
            "</testsuite></testsuites>\n",
            encoding="utf-8",
        )
    (evidence / "runner-name.txt").write_text(
        f"{private_runner}\n",
        encoding="utf-8",
    )
    (evidence / "commit-sha.txt").write_text(
        "0" * 40 + "\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": private_home,
            "HOSTNAME": private_runner,
            "RUNNER_NAME": private_runner,
            "USER": "private-signal-bot-user",
        }
    )
    result = subprocess.run(
        [sys.executable, str(SANITIZER), str(evidence)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    status = json.loads((evidence / "privacy-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "PASS"
    assert status["credential_findings"] == 0
    assert status["replacements"] >= 3

    for name in ("full-tests.xml", "tests.xml"):
        root = ET.parse(evidence / name).getroot()
        suites = root.findall(".//testsuite")
        assert suites
        assert {suite.attrib.get("hostname") for suite in suites} == {"redacted-runner"}

    payload = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(evidence.iterdir()) if path.is_file()
    )
    assert private_runner not in payload
    assert private_home not in payload


def test_signal_bot_workflow_closes_privacy_before_release_publication() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    package = text.index("Package and validate Signal Bot evidence wheel")
    privacy = text.index("Sanitize and verify Signal Bot evidence privacy")
    inventory = text.index("Record sanitized closed evidence inventory")
    stage = text.index("Stage Signal Bot Heavy release evidence")
    publish = text.index("Publish exact Signal Bot Heavy evidence")
    enforce = text.index("Enforce Signal Bot evidence publication gate")
    diagnostic = text.index("Upload Signal Bot Heavy failure diagnostics")
    assert package < privacy < inventory < stage < publish < enforce < diagnostic

    assert "python scripts/sanitize_ci_evidence.py evidence" in text
    assert "rm -f dist/.gitignore" in text
    assert "id: evidence_privacy" in text
    assert "id: evidence_inventory" in text
    assert "id: evidence_publish" in text
    assert 'hostnames != {"redacted-runner"}' in text
    assert "recorded_paths != expected_paths" in text
    assert "Signal Bot evidence contains symlink" in text
    assert "sha256sum -c evidence/package-sha256.txt" in text
    assert "shasum -a 256 -c evidence/package-sha256.txt" in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "steps.evidence_inventory.outcome == 'success'" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: reports/signal-bot-heavy-release" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert "if: ${{ failure() || cancelled() }}" in text[diagnostic:]
    assert "continue-on-error: true" in text[diagnostic:]
    assert (
        "signal-bot-heavy-audit-${{ inputs.expected_sha }}-"
        "${{ github.run_id }}-attempt${{ github.run_attempt }}" in text
    )
