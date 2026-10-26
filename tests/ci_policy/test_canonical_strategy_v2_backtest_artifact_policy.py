from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "canonical-strategy-v2-backtest.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_canonical_strategy_success_evidence_uses_exact_sha_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in text
    assert text.count("uses: ./.github/actions/publish-release-evidence") == 3
    assert "source_path: ${{ runner.temp }}/canonical-strategy-v2-backtest" in text
    assert "asset_name: canonical-strategy-v2-backtest.tar.gz" in text
    assert "source_path: ${{ runner.temp }}/modular-fvg-composition-parity" in text
    assert "asset_name: modular-fvg-composition-parity.tar.gz" in text
    assert "source_path: ${{ runner.temp }}/wave2a-canonical-evidence" in text
    assert "asset_name: canonical-strategy-v2-research-platform.tar.gz" in text
    assert text.count("commit_sha: ${{ env.EXPECTED_SHA }}") == 3
    assert text.count('draft: "true"') == 3
    assert "Enforce canonical backtest publication gate" in text
    assert "Enforce modular FVG composition publication gate" in text
    assert "Enforce Research Platform publication gate" in text
    assert UPLOAD_ACTION not in text


def test_publication_occurs_only_after_fail_closed_evidence_gates() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    modular_gate = text.index("      - name: Enforce modular FVG composition parity evidence")
    backtest_gate = text.index("      - name: Enforce sanitized fail-closed evidence")
    research_gate = text.index("      - name: Enforce Research Platform evidence")
    backtest_publish = text.index("      - name: Publish exact canonical backtest evidence")
    modular_publish = text.index("      - name: Publish exact modular FVG composition evidence")
    research_publish = text.index("      - name: Publish exact Research Platform evidence")
    cleanup = text.index("      - name: Cleanup canonical strategy temporary data")
    assert (
        modular_gate
        < backtest_gate
        < research_gate
        < backtest_publish
        < modular_publish
        < research_publish
        < cleanup
    )


def test_canonical_evidence_identity_privacy_and_inventory_contracts_remain_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"',
        'diff -rq "$first" "$second"',
        "canonical-strategy-v2-result-v1",
        "63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb",
        "51659c6ed582f2843357bd33fbff35059f9a535dd27f05a585a9e7dfa78365c9",
        "evidence inventory mismatch",
        "Research Platform evidence inventory mismatch",
        "modular FVG evidence inventory mismatch",
        '"producer_detection_full_corpus_replay") is not True',
        'forbidden = ("/Users/", "/home/", "GoogleDrive-", "My Drive", "Мой диск")',
        "Research Platform evidence must not contain symlinks",
        'if report["paper_only"] is not True or report["live_trading"] is not False',
    ):
        assert required in text


def test_raw_official_corpus_is_not_a_release_source_and_cleanup_is_unconditional() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    publish_start = text.index("      - name: Publish exact canonical backtest evidence")
    cleanup_start = text.index("      - name: Cleanup canonical strategy temporary data")
    publication = text[publish_start:cleanup_start]
    cleanup = text[cleanup_start:]

    assert "canonical-v2-official-dataset" not in publication
    assert "BTCUSDT_5m.parquet" not in publication
    assert "if: always()" in cleanup
    for required in (
        '"$RUNNER_TEMP/canonical-v2-official-dataset"',
        '"$RUNNER_TEMP/canonical-v2-backtest-a"',
        '"$RUNNER_TEMP/canonical-v2-backtest-b"',
        '"$RUNNER_TEMP/canonical-strategy-v2-backtest"',
        '"$RUNNER_TEMP/modular-fvg-composition-parity"',
        '"$RUNNER_TEMP/wave2a-canonical-work"',
        '"$RUNNER_TEMP/wave2a-canonical-evidence"',
        '"$RUNNER_TEMP/canonical-v2-dataset-command.json"',
        '"$RUNNER_TEMP/modular-fvg-composition-command.json"',
        '"$RUNNER_TEMP/wave2a-canonical-command.json"',
    ):
        assert required in cleanup
