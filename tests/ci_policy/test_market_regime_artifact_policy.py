from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "market-regime-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_market_regime_fast_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    upload_marker = "      - uses: actions/upload-artifact@"
    cleanup_marker = "      - name: Clean temporary state"

    assert text.count(UPLOAD_ACTION) == 1
    assert text.index(upload_marker) < text.index(cleanup_marker)

    upload = text[text.index(upload_marker) : text.index(cleanup_marker)]
    cleanup = text[text.index(cleanup_marker) :]

    assert "if: ${{ failure() || cancelled() }}" in upload
    assert "continue-on-error: true" in upload
    assert UPLOAD_ACTION in upload
    assert "market-regime-fast-${{ matrix.python-version }}" in upload
    assert "if-no-files-found: warn" in upload
    assert "retention-days:" not in upload
    assert "if: always()" in cleanup
