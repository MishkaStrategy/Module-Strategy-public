from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "research-runner-release.yml"


def test_research_runner_release_junit_hostname_is_redacted(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence" / "research-runner-release"
    evidence.mkdir(parents=True)
    private_runner = "private-research-release-runner"
    private_home = "/Users/private-research-release-user"

    (evidence / "research-runner-optimized-tests.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="1" failures="0" errors="0" '
        f'hostname="{private_runner}"><testcase name="test_ok" />'
        "</testsuite></testsuites>\n",
        encoding="utf-8",
    )
    (evidence / "validate-smoke.txt").write_text(
        f"workspace={private_home}/runner\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": private_home,
            "HOSTNAME": private_runner,
            "RUNNER_NAME": private_runner,
            "USER": "private-research-release-user",
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

    root = ET.parse(evidence / "research-runner-optimized-tests.xml").getroot()
    suites = root.findall(".//testsuite")
    assert suites
    assert {suite.attrib.get("hostname") for suite in suites} == {"redacted-runner"}

    payload = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(evidence.iterdir()) if path.is_file()
    )
    assert private_runner not in payload
    assert private_home not in payload


def test_research_runner_release_workflow_closes_scope_before_release_publication() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    prepare = text.index("Prepare exact Research Runner release evidence")
    tests = text.index("Run complete suite with optimized Python")
    reproducibility = text.index("Verify reproducible wheel and sdist")
    privacy = text.index("Sanitize and verify release evidence")
    inventory = text.index("Record closed Research Runner release inventory")
    assemble = text.index("Assemble exact Research Runner release evidence")
    publish = text.index("Publish exact Research Runner release evidence")
    enforce = text.index("Enforce Research Runner release publication gate")
    assert prepare < tests < reproducibility < privacy < inventory < assemble < publish < enforce

    assert "EVIDENCE_DIR: evidence/research-runner-release" in text
    assert 'python scripts/sanitize_ci_evidence.py "$EVIDENCE_DIR"' in text
    assert "/reports/*" not in text
    assert "/reports/**" not in text
    assert "release-evidence/research-runner-release" in text
    assert 'cp -R "$EVIDENCE_DIR" "$bundle/evidence/research-runner-release"' in text
    assert 'cp -R dist/core "$bundle/dist/core"' in text
    assert 'cp -R dist/research_runner "$bundle/dist/research_runner"' in text
    assert 'cp -R dist/research_runner_sdist "$bundle/dist/research_runner_sdist"' in text
    assert 'hostnames != {"redacted-runner"}' in text
    assert "Research Runner release inventory is not closed" in text
    assert "Research Runner release contains symlink" in text
    assert "unsafe wheel path" in text
    assert "unsafe sdist path" in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "steps.evidence_inventory.outcome == 'success'" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "asset_name: research-runner-release-py${{ matrix.python-version }}.tar.gz" in text
    assert "actions/upload-artifact@" not in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text
