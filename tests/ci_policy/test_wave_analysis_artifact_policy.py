import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "wave-analysis.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_wave_analysis_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Wave Analysis candidate" in text
    assert "Stage bounded Wave Analysis evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: wave-analysis-release-evidence" in text
    assert "asset_name: wave-analysis-evidence-py${{ matrix.python-version }}.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Wave Analysis publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_wave_analysis_release_staging_is_bounded_to_previous_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Wave Analysis evidence"
    publish_marker = "      - name: Publish exact Wave Analysis evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    for required in (
        "wave-analysis-junit.xml",
        "wave-analysis-causality-junit.xml",
        "wave-analysis-coverage.json",
        "wave-analysis-dist-checksums.sha256",
        "wave-analysis-security-status.txt",
        "wave-analysis-benchmark.json",
        "reports/wave-analysis-repository",
        "dist/core/*",
        "dist/wave/*",
    ):
        assert required in staging
    for forbidden in (
        ".wave-wheel-venv",
        "GITHUB_WORKSPACE",
        "RUNNER_TEMP",
        'cp -R . "$stage"',
        "modules/wave_analysis/src",
    ):
        assert forbidden not in staging


def test_wave_analysis_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Wave Analysis failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "reports/wave-analysis-repository/**" in diagnostic
    assert "dist/core/*" in diagnostic
    assert "dist/wave/*" in diagnostic
    assert "Cleanup temporary environments" in diagnostic
    assert "wave-analysis-release-evidence" in diagnostic


def test_wave_analysis_quality_security_and_benchmark_gates_remain_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        'matrix:\n        python-version: ["3.12", "3.13"]',
        "coverage report --fail-under=95",
        "Explicit no-look-ahead and lifecycle gate",
        "Central repository compatibility",
        "Clean wheel installation and entry-point smoke",
        "Security scan and SBOM",
        "pip-audit --format json --output wave-analysis-pip-audit.json",
        "Deterministic benchmark smoke",
        "Distribution checksums",
    ):
        assert required in text


def test_wave_analysis_uv_cache_is_unique_to_each_matrix_job() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    configure = "      - name: Configure job-local uv cache"
    setup = "      - name: Set up pinned uv and managed Python"
    configure_start = text.index(configure)
    setup_start = text.index(setup)
    cache_setup = text[configure_start:setup_start]

    assert configure_start < setup_start
    assert (
        'suffix="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_JOB}-py'
        '${{ matrix.python-version }}"'
    ) in cache_setup
    assert (
        'echo "UV_CACHE_DIR=$RUNNER_TEMP/wave-analysis-uv-cache-$suffix" >> "$GITHUB_ENV"'
    ) in cache_setup
    assert "enable-cache: false" in text[setup_start:]
    uv_invocations = [match.start() for match in re.finditer(r"(?m)^\s+uv\s", text)]
    assert uv_invocations
    assert all(position > configure_start for position in uv_invocations)
