from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-heavy.yml"


def test_research_runner_heavy_junit_hostname_is_redacted(tmp_path: Path) -> None:
    evidence = tmp_path / "research-runner-heavy"
    evidence.mkdir()
    private_runner = "private-research-heavy-runner"
    private_home = "/Users/private-research-heavy-user"

    (evidence / "research-runner-stress.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="2" failures="0" errors="0" '
        f'hostname="{private_runner}"><testcase name="first" />'
        '<testcase name="second" /></testsuite></testsuites>\n',
        encoding="utf-8",
    )
    (evidence / "research-runner-stress.log").write_text(
        f"workspace={private_home}/runner\n",
        encoding="utf-8",
    )
    (evidence / "research-runner-stress-environment.json").write_text(
        json.dumps({"head": "0" * 40, "run_id": "1", "scale": 5}) + "\n",
        encoding="utf-8",
    )
    (evidence / "commit-sha.txt").write_text("0" * 40 + "\n", encoding="utf-8")
    (evidence / "workflow-run-id.txt").write_text("1\n", encoding="utf-8")

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": private_home,
            "HOSTNAME": private_runner,
            "RUNNER_NAME": private_runner,
            "USER": "private-research-heavy-user",
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
    privacy = json.loads((evidence / "privacy-status.json").read_text(encoding="utf-8"))
    assert privacy["status"] == "PASS"
    assert privacy["credential_findings"] == 0
    assert privacy["replacements"] >= 2

    root = ET.parse(evidence / "research-runner-stress.xml").getroot()
    suites = root.findall(".//testsuite")
    assert suites
    assert {suite.attrib.get("hostname") for suite in suites} == {"redacted-runner"}

    payload = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(evidence.iterdir()) if path.is_file()
    )
    assert private_runner not in payload
    assert private_home not in payload


def test_research_runner_heavy_workflow_closes_evidence_before_release_publication() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    prepare = text.index("Prepare exact Research Runner Heavy evidence")
    stress = text.index("Run deterministic high-volume and recovery checks")
    privacy = text.index("Sanitize and verify Research Runner Heavy evidence privacy")
    inventory = text.index("Record closed Research Runner Heavy evidence inventory")
    publish = text.index("Publish exact Research Runner Heavy evidence")
    enforce = text.index("Enforce Research Runner Heavy evidence publication gate")
    assert prepare < stress < privacy < inventory < publish < enforce

    assert "EVIDENCE_DIR: reports/research-runner-heavy" in text
    assert 'python scripts/sanitize_ci_evidence.py "$EVIDENCE_DIR"' in text
    assert "research-runner-heavy-${{ github.run_id }}/reports/*" not in text
    assert "research-runner-heavy-${{ github.run_id }}/reports/research-runner-heavy" in text
    assert 'hostnames != {"redacted-runner"}' in text
    assert "Research Runner Heavy checksum inventory is not closed" in text
    assert "Research Runner Heavy evidence contains symlink" in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "steps.evidence_inventory.outcome == 'success'" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "asset_name: research-runner-heavy.tar.gz" in text
    assert "actions/upload-artifact@" not in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text
