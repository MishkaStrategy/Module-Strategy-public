from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "backtest-engine-heavy.yml"
HISTORICAL_WORKFLOW = ROOT / ".github" / "workflows" / "heavy-backtest.yml"


def test_heavy_evidence_redacts_runner_identity_and_local_paths(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private_runner = "private-heavy-runner"
    private_user = "private-heavy-user"
    private_home = f"/Users/{private_user}"

    (evidence / "environment.json").write_text(
        json.dumps(
            {
                "runner_name": private_runner,
                "workspace": f"{private_home}/work/repository",
                "source_uri": f"file://{private_home}/market-data",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (evidence / "runner.txt").write_text(
        f"Darwin {private_runner} 24.0.0 arm64\n",
        encoding="utf-8",
    )
    (evidence / "backtest-tests.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite tests="1" failures="0" errors="0" '
        f'hostname="{private_runner}"><testcase name="test_ok" />'
        "</testsuite></testsuites>\n",
        encoding="utf-8",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": private_home,
            "HOSTNAME": private_runner,
            "RUNNER_NAME": private_runner,
            "USER": private_user,
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
    assert status["files_scanned"] == 3
    assert status["credential_findings"] == 0
    assert status["replacements"] >= 4

    payload = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(evidence.iterdir()) if path.is_file()
    )
    assert private_runner not in payload
    assert private_user not in payload
    assert private_home not in payload
    assert "file://" not in payload
    assert "redacted-runner" in payload

    provenance = json.loads((evidence / "environment.json").read_text(encoding="utf-8"))
    assert provenance["runner_name"] == "redacted-runner"
    junit = (evidence / "backtest-tests.xml").read_text(encoding="utf-8")
    assert 'hostname="redacted-runner"' in junit


def test_heavy_workflow_sanitizes_before_checksums_and_upload() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    runtime = text.index("Validate local Backtest Heavy runtime")
    structured = text.index("Prepare structured evidence for privacy sanitization")
    privacy = text.index("Sanitize and verify Heavy Audit evidence privacy")
    inventory = text.index("Record sanitized closed evidence inventory")
    upload = text.index("Upload exact Heavy Audit evidence")
    assert runtime < structured < privacy < inventory < upload
    assert "bash scripts/runner_preflight.sh backtester" in text
    assert "command -v python3.12" in text
    assert "sys.version_info[:2] == (3, 12)" in text
    assert "./.github/actions/python-runner-preflight" not in text
    assert "python3 scripts/sanitize_ci_evidence.py evidence" in text
    assert "id: evidence_privacy" in text
    assert "id: evidence_inventory" in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "steps.evidence_inventory.outcome == 'success'" in text
    assert "evidence-privacy.json" not in text
    assert (
        "backtest-engine-heavy-${{ inputs.expected_sha }}-"
        "${{ github.run_id }}-attempt${{ github.run_attempt }}" in text
    )


def test_historical_workflow_sanitizes_before_closed_inventory_and_release() -> None:
    text = HISTORICAL_WORKFLOW.read_text(encoding="utf-8")

    recovery = text.index("Verify deterministic restart paths under distinct hash seeds")
    privacy = text.index("Sanitize and verify historical evidence privacy")
    inventory = text.index("Record sanitized closed evidence inventory")
    stage = text.index("Stage historical backtest release evidence")
    publish = text.index("Publish exact historical backtest evidence")
    enforce = text.index("Enforce historical evidence publication gate")
    diagnostic = text.index("Upload historical backtest failure diagnostics")
    assert recovery < privacy < inventory < stage < publish < enforce < diagnostic
    assert "python scripts/sanitize_ci_evidence.py evidence" in text
    assert "id: evidence_privacy" in text
    assert "id: evidence_inventory" in text
    assert "id: evidence_publish" in text
    assert "privacy-status.json" in text
    assert 'privacy.get("status") != "PASS"' in text
    assert 'privacy.get("credential_findings") != 0' in text
    assert "actual_sha != expected_sha" in text
    assert "relative = path.relative_to(evidence).as_posix()" in text
    assert 'checksum_lines.append(f"{digest}  {relative}")' in text
    assert 'checksum_lines.append(f"{digest}  evidence/{relative}")' not in text
    assert "recorded_paths != expected_paths" in text
    assert "source_path: reports/historical-backtest-release" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert "path: evidence\n" in text
    assert "path: evidence/**" not in text
    assert "steps.evidence_privacy.outcome == 'success'" in text
    assert "steps.evidence_inventory.outcome == 'success'" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = "success"' in text
    assert "if: ${{ failure() || cancelled() }}" in text[diagnostic:]
    assert "continue-on-error: true" in text[diagnostic:]
    assert (
        "historical-strategy-backtest-${{ inputs.expected_sha }}-"
        "${{ github.run_id }}-attempt${{ github.run_attempt }}" in text
    )
