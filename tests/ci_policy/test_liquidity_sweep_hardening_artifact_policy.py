from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "liquidity-sweep-hardening-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_liquidity_sweep_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Liquidity Sweep hardening candidate" in text
    assert "Stage bounded Liquidity Sweep hardening evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: liquidity-sweep-release-evidence" in text
    assert "asset_name: liquidity-sweep-hardening-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Liquidity Sweep hardening publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_liquidity_sweep_staging_is_bounded_and_matrix_aware() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Liquidity Sweep hardening evidence"
    publish_marker = "      - name: Publish exact Liquidity Sweep hardening evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="liquidity-sweep-release-evidence"' in staging
    assert '[[ "${{ matrix.python-version }}" == "3.13" ]]' in staging
    assert 'cp -R reports/liquidity-sweep-repository "$stage/"' in staging
    assert 'cp -R dist "$stage/"' in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging
    for path in (
        "liquidity-sweep-ruff.txt",
        "liquidity-sweep-mypy.txt",
        "liquidity-sweep-junit.xml",
        "liquidity-sweep-causality-junit.xml",
        "liquidity-sweep-wheel-junit.xml",
        "liquidity-sweep-sdist-junit.xml",
        "liquidity-sweep-coverage.json",
        "liquidity-sweep-coverage.txt",
        "liquidity-sweep-wheel-mypy.txt",
        "liquidity-sweep-dist-checksums.sha256",
    ):
        assert path in staging
    for path in (
        "liquidity-sweep-integration-smoke.txt",
        "liquidity-sweep-bandit.json",
        "liquidity-sweep-secrets.json",
        "liquidity-sweep-pip-audit.json",
        "liquidity-sweep-sbom.cdx.json",
        "liquidity-sweep-benchmark.json",
        "liquidity-sweep-benchmark.stdout.txt",
        "liquidity-sweep-stress-1m.json",
        "reports/liquidity-sweep-repository",
    ):
        assert path in staging


def test_liquidity_sweep_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Liquidity Sweep hardening failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert (
        "liquidity-sweep-evidence-py${{ matrix.python-version }}-${{ github.run_attempt }}"
        in diagnostic
    )
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup temporary environments" in diagnostic
