from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "position-scaling-hardening-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_position_scaling_success_evidence_uses_exact_head_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "integration/modular-strategy-v0.5.0" in text
    assert "integration/modular-strategy-full-rc" in text
    assert "core-ref: integration/modular-strategy-v0.5.0" in text
    assert "core-ref: core/hardening-v0.4.x" in text
    assert 'version("modular-strategy-core") == "0.4.2"' in text
    assert 'version("modular-strategy-core") == "0.4.1"' not in text
    assert 'expected_root = Path("core-under-test/src").resolve()' in text
    assert "actual.is_relative_to(expected_root)" in text
    assert "'pyarrow>=16.0' 'matplotlib>=3.8'" in text
    assert "./modules/entry_model" in text
    assert "./modules/position_exit_management[dev]" in text
    assert "./applications/trading_runner[dev]" in text
    assert "permissions:\n  contents: write" in text
    assert "EXPECTED_HEAD: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in text
    assert "Verify exact Position Scaling candidate" in text
    assert "Stage bounded Position Scaling hardening evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "source_path: position-scaling-release-evidence" in text
    assert (
        "asset_name: position-scaling-evidence-py${{ matrix.python-version }}-"
        "${{ matrix.core-label }}.tar.gz" in text
    )
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in text
    assert 'draft: "true"' in text
    assert "Enforce Position Scaling publication gate" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in text


def test_position_scaling_hardening_core_compatibility_is_isolated() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Full repository compatibility suite"
    build_marker = "      - name: Build Core and module distributions"
    compatibility = text[text.index(marker) : text.index(build_marker)]

    assert 'compat_src=".position-scaling-compat-src"' in compatibility
    assert 'candidate_venv=".position-scaling-candidate-venv"' in compatibility
    assert "for package in modular_backtest signal_bot strategy_modules; do" in compatibility
    assert 'cp -R "src/$package" "$compat_src/"' in compatibility
    assert "src/modular_core" in compatibility
    assert "cp -R src/modular_core" not in compatibility
    assert 'export PYTHONPATH="$PWD/$compat_src"' in compatibility
    assert 'candidate_root = Path("src/modular_core").resolve()' in compatibility
    assert "not actual.is_relative_to(candidate_root)" in compatibility
    assert '-o "pythonpath=$compat_src" tests' in compatibility
    assert "./modules/market_regime[dev]" in compatibility
    assert "--ignore=tests/breakout_return/test_packaging_contract.py" in compatibility
    assert "tests/breakout_return/test_packaging_contract.py" in compatibility
    assert "breakout-return-packaging-junit.xml" in compatibility
    assert 'PYTHONPATH="" "$candidate_venv/bin/python" -m pytest' in compatibility

    # Frozen Core 0.4.2 cannot satisfy post-release Core contracts. The family
    # is excluded only from the legacy run and is blocking in the candidate run.
    assert "--ignore-glob='tests/test_post_release_*.py'" in compatibility
    assert compatibility.count("tests/test_post_release_*.py") == 2
    for old_exception in (
        "--ignore=tests/test_post_release_secret_repr_redaction.py",
        "--ignore=tests/test_post_release_serialization_security.py",
        "--ignore=tests/test_post_release_setup_registry_followup.py",
    ):
        assert old_exception not in compatibility

    # Current-Core-only contracts remain covered exactly once in each side of
    # the boundary: deselected from the frozen Core and required on candidate Core.
    assert compatibility.count("tests/test_broker_state.py") == 2
    assert compatibility.count("tests/test_hardening_v042_outputs.py") == 2
    current_only_nodes = (
        "tests/backtest_engine/test_recovery.py::test_runtime_snapshot_restart_matches_single_pass_without_prefix_resimulation",
        "tests/backtest_engine/test_recovery.py::test_runtime_snapshot_releases_first_module_before_fresh_factory",
        "tests/backtest_engine/test_recovery.py::test_runtime_snapshot_rejects_changed_continuation_boundary",
        "tests/backtest_engine/test_recovery.py::test_runtime_snapshot_fails_closed_on_payload_corruption",
        "tests/backtest_engine/test_mark_valuation_runner_v1.py::test_mark_equal_last_reproduces_core_result_digest_exactly",
        "tests/backtest_engine/test_mark_valuation_runner_v1.py::test_mark_not_equal_last_changes_equity_but_not_traded_fills",
        "tests/backtest_engine/test_mark_valuation_runner_v1.py::test_real_runtime_snapshot_restores_mark_broker_exactly",
        "tests/backtest_engine/test_funding_runner_v3.py::test_real_runtime_snapshot_restores_funding_broker_exactly",
        "tests/backtest_engine/test_scalability.py::test_scalable_engine_aggregates_warmup_skips_and_keeps_exact_equity",
        "tests/backtest_engine/test_scalability.py::test_scalable_engine_resource_limit_is_fail_closed",
        "tests/backtest_engine/test_scalability.py::test_scalable_engine_preserves_trade_and_critical_event_semantics",
        "tests/backtest_engine/test_timeframe_scalability.py::test_snapshot_view_is_reused_until_a_completed_htf_bar_arrives",
        "tests/backtest_engine/test_timeframe_scalability.py::test_chunked_compaction_bounds_visible_history_and_preserves_saved_views",
    )
    for node_id in current_only_nodes:
        assert compatibility.count(node_id) == 2


def test_position_scaling_security_audit_is_isolated_and_patched() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Security scan and SBOM"
    benchmark_marker = "      - name: Deterministic benchmark and memory evidence"
    security = text[text.index(marker) : text.index(benchmark_marker)]

    assert 'security_venv=".position-scaling-security-venv"' in security
    assert "-e 'core-under-test[dev]'" in security
    assert "-e 'modules/position_scaling[dev]'" in security
    assert "'pytest>=9.0.3,<10'" in security
    assert '"$security_venv/bin/python" -m pip check' in security
    assert 'minimum = Version("9.0.3")' in security
    assert '"$security_venv/bin/bandit"' in security
    assert '"$security_venv/bin/detect-secrets"' in security
    assert '"$security_venv/bin/pip-audit"' in security
    assert '"$security_venv/bin/cyclonedx-py" environment "$security_venv/bin/python"' in security


def test_position_scaling_staging_is_bounded_and_matrix_aware() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Stage bounded Position Scaling hardening evidence"
    publish_marker = "      - name: Publish exact Position Scaling hardening evidence"
    staging = text[text.index(marker) : text.index(publish_marker)]

    assert 'stage="position-scaling-release-evidence"' in staging
    assert '[[ "${{ matrix.python-version }}" == "3.13" ]]' in staging
    for path in (
        "pytest-junit.xml",
        "coverage-report.txt",
        "coverage.json",
        "distribution-checksums.sha256",
        "core-dist",
        "module-dist",
        "benchmark_results.json",
        "profile_top20.txt",
        "repository-root-junit.xml",
        "current-core-compat-junit.xml",
        "breakout-return-packaging-junit.xml",
        "session-range-junit.xml",
        "market-regime-junit.xml",
        "fvg-junit.xml",
        "bandit.json",
        "detect-secrets.json",
        "pip-audit.json",
        "sbom.cdx.json",
    ):
        assert path in staging
    assert "core-under-test" not in staging
    assert ".ci-venv" not in staging
    assert ".wheel-venv" not in staging
    assert ".position-scaling-compat-src" not in staging
    assert ".position-scaling-candidate-venv" not in staging
    assert ".position-scaling-security-venv" not in staging
    assert 'cp -R . "$stage"' not in staging
    assert "GITHUB_WORKSPACE" not in staging


def test_position_scaling_actions_artifact_is_failure_only_diagnostic() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    marker = "      - name: Upload Position Scaling failure diagnostics"
    diagnostic = text[text.index(marker) :]

    assert text.count(UPLOAD_ACTION) == 1
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert (
        "position-scaling-evidence-py${{ matrix.python-version }}-${{ matrix.core-label }}"
        in diagnostic
    )
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in diagnostic
    assert "Cleanup temporary environments" in diagnostic
    assert "position-scaling-release-evidence" in diagnostic
    assert ".position-scaling-compat-src" in diagnostic
    assert ".position-scaling-candidate-venv" in diagnostic
    assert ".position-scaling-security-venv" in diagnostic
