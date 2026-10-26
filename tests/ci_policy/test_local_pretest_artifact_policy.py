from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "local-pretest-tooling-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_local_pretest_format_artifact_is_failure_only_after_enforcement() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    enforce_marker = "      - name: Enforce format"
    tests_marker = "      - name: Tests"
    cli_marker = "      - name: CLI help"
    upload_marker = "      - name: Upload format failure diagnostics"

    assert text.count(UPLOAD_ACTION) == 1
    assert text.index(enforce_marker) < text.index(tests_marker)
    assert text.index(tests_marker) < text.index(cli_marker)
    assert text.index(cli_marker) < text.index(upload_marker)

    upload = text[text.index(upload_marker) :]
    assert "if: ${{ failure() || cancelled() }}" in upload
    assert "continue-on-error: true" in upload
    assert UPLOAD_ACTION in upload
    assert "local-pretest-format-${{ matrix.python-version }}" in upload
    assert "format-${{ matrix.python-version }}.txt" in upload
    assert "format-${{ matrix.python-version }}.status" in upload
    assert "if-no-files-found: error" in upload
    assert "retention-days:" not in upload
