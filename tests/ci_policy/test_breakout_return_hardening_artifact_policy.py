from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "breakout-return-hardening-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_breakout_return_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Breakout Return hardening candidate" in text
    assert "Stage bounded Breakout Return hardening evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: breakout-return-release-evidence" in text
    assert "asset_name: breakout-return-hardening-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Breakout Return hardening publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_breakout_return_staging_is_bounded_and_matrix_aware() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Breakout Return hardening evidence"
    publish_marker = "      - name: Publish exact Breakout Return hardening evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="breakout-return-release-evidence"' in staging
    assert '[[ "${{ matrix.python-version }}" == "3.13" ]]' in staging
    assert 'cp -R dist "$stage/"' in staging
    assert 'cp -R reports/breakout-return-repository "$stage/reports/"' in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging
    for path in (
        "breakout-return-mypy.txt",
        "breakout-return-junit.xml",
        "breakout-return-coverage.json",
        "breakout-return-coverage.txt",
    ):
        assert path in staging
    for path in (
        "breakout-return-benchmark.json",
        "breakout-return-secrets.json",
        "breakout-return-pip-audit.json",
        "breakout-return-sbom.cdx.json",
        "breakout-return-dist-checksums.sha256",
        "reports/breakout-return-repository",
    ):
        assert path in staging


def test_breakout_return_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Breakout Return hardening failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert (
        "breakout-return-evidence-py${{ matrix.python-version }}-${{ github.run_attempt }}"
        in diagnostic
    )
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup temporary environments" in diagnostic
    assert "breakout-return-release-evidence" in diagnostic
