from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/fibonacci-module-ci.yml")
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_fibonacci_success_evidence_uses_exact_head_release_publication() -> None:
    text = _workflow_text()

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Fibonacci candidate" in text
    assert "Record Fibonacci evidence identity" in text
    assert "Stage Fibonacci release evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "id: evidence_publish" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert "Enforce Fibonacci publication gate" in text


def test_fibonacci_actions_upload_is_failure_cancelled_diagnostic_only() -> None:
    text = _workflow_text()

    assert text.count(UPLOAD_ACTION) == 1
    assert "- name: Upload Fibonacci evidence" not in text
    marker = "- name: Upload Fibonacci failure diagnostics"
    assert marker in text
    diagnostic = text[text.index(marker) :]
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in text
    assert text.index("Enforce Fibonacci publication gate") < text.index(marker)
