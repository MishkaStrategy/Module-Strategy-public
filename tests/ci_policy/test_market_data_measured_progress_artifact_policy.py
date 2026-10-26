from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "market-data-measured-progress-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_measured_progress_is_bound_to_exact_candidate_head() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Checkout exact measured-progress candidate" in text
    assert "Verify exact measured-progress candidate" in text
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"' in text
    assert "persist-credentials: false" in text


def test_measured_progress_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload measured-progress failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "path: modules/market_data_ingestion/reports/measured-progress-*.log" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "publish-release-evidence" not in text


def test_measured_progress_quality_and_privacy_gates_remain_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        'python-version: ["3.12", "3.13"]',
        "Compile measured progress slice",
        "Ruff focused lint",
        "Ruff focused format",
        "Strict MyPy packages",
        "Enforce exact feature boundary",
        "Reject public progress source-location leakage",
        "Run measured progress and application regression",
        "tests/test_measured_progress.py",
        "tests/test_application_*.py",
    ):
        assert required in text
