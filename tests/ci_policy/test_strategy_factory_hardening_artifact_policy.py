from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "strategy-factory-hardening.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_strategy_factory_hardening_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Strategy Factory hardening candidate" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "asset_name: strategy-factory-hardening.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Strategy Factory hardening publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_strategy_factory_hardening_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload hardening failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "strategy-factory-hardening-${{ github.run_id }}" in diagnostic
    for path in (
        "strategy-factory-fault-injection.xml",
        "strategy-factory-soak.log",
        "strategy-factory-performance.json",
        "strategy-factory-performance.log",
        "strategy-factory-safety-boundary.xml",
        "strategy-factory-safety-boundary.log",
    ):
        assert path in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
