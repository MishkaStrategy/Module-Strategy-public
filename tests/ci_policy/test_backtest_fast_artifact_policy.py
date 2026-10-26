from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "backtest-engine-fast.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_backtest_fast_diagnostics_are_failure_only_after_enforcement() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    gate_marker = "      - name: Enforce quality gates"
    upload_marker = "      - name: Upload quality diagnostics"

    assert text.count(UPLOAD_ACTION) == 1
    assert text.index(gate_marker) < text.index(upload_marker)

    upload = text[text.index(upload_marker) :]
    assert "if: ${{ failure() || cancelled() }}" in upload
    assert "continue-on-error: true" in upload
    assert UPLOAD_ACTION in upload
    assert "backtest-engine-fast-diagnostics-${{ matrix.python-version }}" in upload
    assert "if-no-files-found: error" in upload
    assert "retention-days:" not in upload
