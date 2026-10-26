from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-execution-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
FROZEN_ROUTER_SHA = "f779e599b361a7b860506980de6b5ae8d0cd1f01"


def test_paper_execution_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Paper Execution candidate" in text
    assert FROZEN_ROUTER_SHA in text
    assert "Stage bounded Paper Execution component evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: paper-execution-release-evidence" in text
    assert "asset_name: paper-execution-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Paper Execution publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_paper_execution_staging_preserves_bounded_old_evidence_scope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Paper Execution component evidence"
    publish_marker = "      - name: Publish exact Paper Execution component evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="paper-execution-release-evidence"' in staging
    assert "test -d dist/paper_execution-component" in staging
    assert "test -d reports/paper_execution" in staging
    assert 'cp -R dist/paper_execution-component "$stage/dist/"' in staging
    assert 'cp -R reports/paper_execution "$stage/"' in staging
    assert ".contract-sources" not in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging


def test_paper_execution_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Paper Execution failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "name: paper-execution-py${{ matrix.python-version }}" in diagnostic
    assert "dist/paper_execution-component/*" in diagnostic
    assert "reports/paper_execution/*" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup Paper Execution evidence staging" in diagnostic
