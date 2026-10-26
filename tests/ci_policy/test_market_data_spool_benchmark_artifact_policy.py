from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
IO = WORKFLOWS / "market-data-ingestion-io-benchmark.yml"
MULTI = WORKFLOWS / "market-data-ingestion-multisymbol-benchmark.yml"
PUBLISH = "uses: ./.github/actions/publish-release-evidence"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_exact_pr_source_identity() -> None:
    required = (
        "permissions:\n  contents: write",
        "ref: ${{ github.event.pull_request.head.sha }}",
        "ref: ${{ github.event.pull_request.base.sha }}",
        'git rev-parse HEAD)" = "$HEAD_SHA"',
        'git -C _baseline rev-parse HEAD)" = "$BASE_SHA"',
    )
    for workflow in (IO, MULTI):
        text = _read(workflow)
        for marker in required:
            assert marker in text


def test_success_evidence_uses_release_assets() -> None:
    io = _read(IO)
    multi = _read(MULTI)
    assert io.count(PUBLISH) == 2
    assert multi.count(PUBLISH) == 1
    assert "asset_name: market-data-ingestion-io-benchmark.tar.gz" in io
    assert "market-data-ingestion-real-btc-io-benchmark.tar.gz" in io
    assert "market-data-ingestion-multisymbol-benchmark.tar.gz" in multi
    assert io.count("commit_sha: ${{ env.HEAD_SHA }}") == 2
    assert multi.count("commit_sha: ${{ env.HEAD_SHA }}") == 1
    assert io.count('draft: "true"') == 2
    assert multi.count('draft: "true"') == 1
    assert "actions/upload-artifact@" not in io
    assert "actions/upload-artifact@" not in multi


def test_privacy_gates_precede_publication() -> None:
    io = _read(IO)
    multi = _read(MULTI)
    io_gate = "Enforce synthetic benchmark evidence privacy and inventory"
    io_publish = "Publish exact synthetic benchmark evidence"
    real_gate = "Enforce real BTC evidence privacy and inventory"
    real_publish = "Publish exact real BTC benchmark evidence"
    multi_gate = "Enforce multi-symbol evidence privacy and inventory"
    multi_publish = "Publish exact multi-symbol benchmark evidence"
    assert io.index(io_gate) < io.index(io_publish)
    assert io.index(real_gate) < io.index(real_publish)
    assert multi.index(multi_gate) < multi.index(multi_publish)

    privacy_markers = (
        '"/Users/"',
        '"/home/"',
        '"GoogleDrive-"',
        '"My Drive"',
        '"Мой диск"',
        '"RUNNER_TEMP"',
        '"runner.temp"',
        '"file://"',
    )
    for text in (io, multi):
        assert "path.is_symlink()" in text
        assert "stat.S_ISREG" in text
        for marker in privacy_markers:
            assert marker in text


def test_evidence_identity_and_inventory_are_fail_closed() -> None:
    io = _read(IO)
    multi = _read(MULTI)
    base_check = 'comparison.get("base_sha") != os.environ["BASE_SHA"]'
    head_check = 'comparison.get("head_sha") != os.environ["HEAD_SHA"]'
    for text in (io, multi):
        assert base_check in text
        assert head_check in text
        assert "evidence inventory mismatch" in text

    assert 'source.get("name") != "BTCUSDT_5m.parquet"' in io
    assert 'source.get("sha256") != os.environ["INPUT_SHA256"]' in io
    assert 'source.get("size_bytes")' in io


def test_raw_btc_source_is_not_published() -> None:
    text = _read(IO)
    start = text.index("Publish exact real BTC benchmark evidence")
    end = text.index("Cleanup real BTC benchmark state")
    publication = text[start:end]
    cleanup = text[end:]
    assert "mdi-btc-corpus-path.txt" not in publication
    assert "BTCUSDT_5m.parquet" not in publication
    assert "reports/real-btc-io" in publication
    assert 'rm -f "$RUNNER_TEMP/mdi-btc-corpus-path.txt"' in cleanup
    assert '"$RUNNER_TEMP"/mdi-real-*' in cleanup


def test_benchmark_semantics_and_routing_are_preserved() -> None:
    io = _read(IO)
    multi = _read(MULTI)
    io_markers = (
        "runs-on: [self-hosted, fast]",
        "runs-on: [self-hosted, backtester]",
        "for rows in 20000 100000; do",
        "for repetition in 1 2 3; do",
        "for repetition in 1 2; do",
        "--batch-size 8192",
        "expected_rows == {721_597}",
        "expected_partitions == {83}",
    )
    for marker in io_markers:
        assert marker in io

    multi_markers = (
        "runs-on: [self-hosted, fast]",
        "for repetition in 1 2 3; do",
        "--symbols ADAUSDT BTCUSDT ETHUSDT",
        "--rows-per-symbol 8928",
        '"total_rows": 26_784',
    )
    for marker in multi_markers:
        assert marker in multi


def test_cleanup_is_unconditional() -> None:
    io = _read(IO)
    multi = _read(MULTI)
    for name in (
        "Cleanup synthetic benchmark state",
        "Cleanup real BTC benchmark state",
    ):
        start = io.index(name)
        assert "if: always()" in io[start : start + 200]

    start = multi.index("Cleanup multi-symbol benchmark state")
    assert "if: always()" in multi[start : start + 200]
    assert '"$RUNNER_TEMP"/mdi-multisymbol-*' in multi
