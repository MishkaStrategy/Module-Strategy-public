from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "results-aggregator-fast.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_results_aggregator_fast_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "results-aggregator-fast-${{ matrix.python-version }}" in diagnostic
    assert "results-coverage-${{ matrix.python-version }}.json" in diagnostic
    assert "results-security-${{ matrix.python-version }}.json" in diagnostic
    assert "dist/results_aggregator/*.whl" in diagnostic
    assert "if-no-files-found: error" in diagnostic
    assert "retention-days:" not in diagnostic
