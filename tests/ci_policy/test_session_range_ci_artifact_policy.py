from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "session-range-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_session_range_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Session Range candidate" in text
    assert "Stage bounded Session Range evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: session-range-release-evidence" in text
    assert "asset_name: session-range-ci-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Session Range publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_session_range_staging_preserves_bounded_old_evidence_scope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Session Range evidence"
    publish_marker = "      - name: Publish exact Session Range evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="session-range-release-evidence"' in staging
    assert "test -d reports/session-range-ci" in staging
    assert "test -d dist/session-range-ci" in staging
    assert 'cp -R reports/session-range-ci "$stage/reports"' in staging
    assert 'cp -R dist/session-range-ci "$stage/dist"' in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging


def test_session_range_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Session Range failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "session-range-ci-py${{ matrix.python-version }}-${{ github.run_attempt }}" in diagnostic
    assert "reports/session-range-ci/**" in diagnostic
    assert "dist/session-range-ci/**" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup Session Range evidence staging" in diagnostic
