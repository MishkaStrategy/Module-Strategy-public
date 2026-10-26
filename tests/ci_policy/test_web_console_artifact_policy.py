from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_web_console_ci_artifact_is_failure_only_diagnostic() -> None:
    text = _read("market-data-web-console-ci.yml")
    marker = "      - name: Upload console evidence"
    upload = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in upload
    assert "continue-on-error: true" in upload
    assert UPLOAD_ACTION in upload
    assert "market-data-web-console-${{ github.sha }}" in upload
    assert "if-no-files-found: warn" in upload
    assert "retention-days:" not in upload


def test_web_console_e2e_artifact_is_failure_only_short_lived_diagnostic() -> None:
    text = _read("market-data-web-console-e2e.yml")
    marker = "      - name: Upload short-lived E2E evidence"
    upload = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in upload
    assert "continue-on-error: true" in upload
    assert UPLOAD_ACTION in upload
    assert "market-data-web-console-e2e-${{ github.sha }}" in upload
    assert "test-results" in upload
    assert "if-no-files-found: warn" in upload
    assert "retention-days:" not in upload
