from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "market-data-ingestion-binance-acceptance.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_binance_acceptance_uses_exact_sha_release_for_success_evidence() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    assert "ref: ${{ env.EXPECTED_SHA }}" in text
    assert "Verify exact requested commit" in text
    assert "Validate bounded public acceptance evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: modules/market_data_ingestion/acceptance-evidence" in text
    assert "asset_name: market-data-binance-public-acceptance.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert 'draft: "true"' in text
    assert "Enforce public acceptance publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_binance_acceptance_has_pre_default_exact_head_bootstrap() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "on:\n  create:\n  workflow_dispatch:" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "github.event.ref_type == 'branch'" in text
    assert "startsWith(github.event.ref, 'acceptance/binance-public-')" in text
    assert (
        "EXPECTED_SHA: ${{ github.event_name == 'workflow_dispatch' && inputs.expected_sha || github.sha }}"
        in text
    )
    assert "ref: ${{ env.EXPECTED_SHA }}" in text


def test_binance_acceptance_success_evidence_is_bounded_and_privacy_checked() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Validate bounded public acceptance evidence"
    publish = "      - name: Publish exact public acceptance evidence"
    validation = text[text.index(marker) : text.index(publish)]

    for required in (
        '"binance.yaml"',
        '"deep-validation.json"',
        '"download-exit-code.txt"',
        '"download.json"',
        '"download.log"',
        '"files.txt"',
        '"head-sha.txt"',
        'os.environ.get("HOME")',
        'os.environ.get("GITHUB_WORKSPACE")',
        'os.environ.get("RUNNER_TEMP")',
        "credential = re.compile",
        "symlink is forbidden",
        "public acceptance evidence head SHA mismatch",
    ):
        assert required in validation
    assert "data/" not in validation


def test_binance_acceptance_staging_excludes_raw_dataset_tree() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Prepare public acceptance evidence"
    validation = "      - name: Validate bounded public acceptance evidence"
    staging = text[text.index(marker) : text.index(validation)]

    assert "if: always()" in staging
    assert "id: evidence_staging" in staging
    assert "cp -R .acceptance/." not in staging
    assert 'cp ".acceptance/$name" "acceptance-evidence/$name"' in staging
    assert "find acceptance-evidence -maxdepth 1 -type f" in staging
    assert ".acceptance/data" not in staging
    assert "private_path_replacements = tuple" in staging
    assert '"<redacted-workspace>"' in staging
    assert '"<redacted-runner-temp>"' in staging
    assert '"<redacted-home>"' in staging
    assert "sanitized = sanitized.replace(value, replacement)" in staging
    assert "unexpected public acceptance diagnostic evidence" in staging
    assert 'os.environ.get("HOME")' in staging
    assert 'os.environ.get("GITHUB_WORKSPACE")' in staging
    assert 'os.environ.get("RUNNER_TEMP")' in staging
    assert "credential = re.compile" in staging
    assert "symlink is forbidden in public acceptance staging" in staging
    for name in (
        "binance.yaml",
        "deep-validation.json",
        "download-exit-code.txt",
        "download.json",
        "download.log",
        "head-sha.txt",
    ):
        assert name in staging


def test_binance_acceptance_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload public acceptance failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert (
        "if: ${{ (failure() || cancelled()) && steps.evidence_staging.outcome == 'success' }}"
    ) in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "path: modules/market_data_ingestion/acceptance-evidence.tar.gz" in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup public acceptance staging" in diagnostic


def test_binance_acceptance_download_and_validation_gates_remain_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        "Generate closed public-data request",
        "Public Binance download",
        "Deep integrity validation",
        "market-data download --config .acceptance/binance.yaml",
        '"market-data",\n              "validate"',
        "symbol: BTCUSDT",
        "base_timeframe: 5m",
        "derived_timeframes: [15m, 1h]",
        "reject_unresolved_required_gaps: true",
    ):
        assert required in text
