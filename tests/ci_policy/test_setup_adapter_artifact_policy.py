from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "setup-adapter-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_setup_adapter_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "integration/modular-strategy-v0.5.0" in text
    assert "integration/modular-strategy-full-rc" in text
    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Setup Adapter candidate" in text
    assert "Stage bounded Setup Adapter evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: setup-adapter-release-evidence" in text
    assert "asset_name: setup-adapter-exact-head-evidence.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Setup Adapter publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_setup_adapter_release_staging_preserves_only_previous_evidence_scope() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Setup Adapter evidence"
    publish_marker = "      - name: Publish exact Setup Adapter evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="setup-adapter-release-evidence"' in staging
    assert "modules/setup_adapter/reports/*" in staging
    assert "dist/setup_adapter/*.whl" in staging
    assert "dist/setup_adapter/*.tar.gz" in staging
    for forbidden in (
        ".deps/entry-model-repo",
        "dist/core",
        "dist/session_range",
        "dist/entry_model",
        "RUNNER_TEMP",
        "GITHUB_WORKSPACE",
        'cp -R . "$stage"',
    ):
        assert forbidden not in staging


def test_setup_adapter_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Setup Adapter failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "name: setup-adapter-exact-head-evidence" in diagnostic
    assert "modules/setup_adapter/reports/*" in diagnostic
    assert "dist/setup_adapter/*.whl" in diagnostic
    assert "dist/setup_adapter/*.tar.gz" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup" in diagnostic
    assert "setup-adapter-release-evidence" in diagnostic
