from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "risk-sizing-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_risk_sizing_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Risk Sizing candidate" in text
    assert "Stage bounded Risk Sizing evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: risk-sizing-release-evidence" in text
    assert (
        "asset_name: risk-sizing-position-adjustments-py${{ matrix.python-version }}.tar.gz" in text
    )
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Risk Sizing publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_risk_sizing_staging_is_bounded_and_matrix_aware() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Risk Sizing evidence"
    publish_marker = "      - name: Publish exact Risk Sizing evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="risk-sizing-release-evidence"' in staging
    assert '[[ "${{ matrix.python-version }}" == "3.13" ]]' in staging
    for path in (
        "static-quality.log",
        "risk-sizing-junit.xml",
        "risk-sizing-coverage.txt",
        "risk-sizing-coverage.json",
        "packaging.log",
        "distribution-checksums.sha256",
        "core-dist",
        "risk-dist",
        "risk-sdist-wheel",
        "bandit.json",
        "detect-secrets.json",
        "pip-audit.json",
        "sbom.cdx.json",
    ):
        assert path in staging
    assert "risk-package-stage" not in staging
    assert ".ci-venv" not in staging
    assert ".wheel-venv" not in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging


def test_risk_sizing_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Risk Sizing failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "name: risk-sizing-position-adjustments-py${{ matrix.python-version }}" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup" in diagnostic
    assert "risk-sizing-release-evidence" in diagnostic
