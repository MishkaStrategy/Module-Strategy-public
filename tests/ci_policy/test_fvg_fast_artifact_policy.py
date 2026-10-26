from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "fvg-module-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_fvg_fast_is_bound_to_exact_candidate_head() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Checkout isolated exact-head FVG workspace" in text
    assert "Verify exact-head FVG candidate" in text
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"' in text
    assert "persist-credentials: false" in text


def test_fvg_fast_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload focused failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "path: .work/fvg-fast/reports/fvg-fast" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "publish-release-evidence" not in text


def test_fvg_fast_quality_gates_remain_blocking() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        "uv pip check",
        "python -m compileall -q plugins/fvg/src plugins/fvg/tests plugins/fvg/scripts",
        "ruff check plugins/fvg/src plugins/fvg/tests plugins/fvg/benchmarks plugins/fvg/scripts",
        "mypy plugins/fvg/src/modular_strategy_fvg",
        "python -m pytest -q plugins/fvg/tests",
        "--junitxml=reports/fvg-fast/junit.xml",
    ):
        assert required in text
