from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "protection-target-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_protection_target_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Protection Target candidate" in text
    assert "Stage bounded Protection Target evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: protection-target-release-evidence" in text
    assert "asset_name: protection-target-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Protection Target publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_protection_target_staging_preserves_bounded_old_evidence_scope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Protection Target evidence"
    publish_marker = "      - name: Publish exact Protection Target evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="protection-target-release-evidence"' in staging
    assert "test -d reports" in staging
    assert "test -d dist/protection-target" in staging
    assert 'cp -R reports "$stage/"' in staging
    assert 'cp -R dist/protection-target "$stage/dist/"' in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging


def test_protection_target_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Protection Target failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert (
        "protection-target-py${{ matrix.python-version }}-${{ github.run_attempt }}" in diagnostic
    )
    assert "reports/**" in diagnostic
    assert "dist/protection-target/**" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup" in diagnostic
    assert "protection-target-release-evidence" in diagnostic
