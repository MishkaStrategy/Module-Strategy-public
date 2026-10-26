from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "position-exit-management-ci.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_position_exit_success_evidence_uses_exact_sha_release() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write\n  issues: write" in text
    assert text.count("uses: ./.github/actions/publish-release-evidence") == 2
    assert "asset_name: position-exit-fast-py${{ matrix.python-version }}.tar.gz" in text
    assert "asset_name: position-exit-full-release.tar.gz" in text
    assert text.count("commit_sha: ${{ env.TARGET_SHA }}") == 2
    assert text.count('draft: "true"') == 2
    assert "Enforce fast evidence publication gate" in text
    assert "Enforce full-release evidence publication gate" in text


def test_position_exit_fast_publication_follows_aggregate_quality_gate() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    enforce = text.index("      - name: Enforce fast-quality results")
    publish = text.index("      - name: Publish exact fast evidence")
    diagnostic = text.index("      - name: Upload fast failure diagnostics")
    assert enforce < publish < diagnostic
    for required in (
        "RUFF_OUTCOME: ${{ steps.ruff.outcome }}",
        "MYPY_OUTCOME: ${{ steps.mypy.outcome }}",
        "TESTS_OUTCOME: ${{ steps.tests.outcome }}",
        "SMOKE_OUTCOME: ${{ steps.smoke.outcome }}",
        'test "${RUFF_OUTCOME}" = success',
        'test "${MYPY_OUTCOME}" = success',
        'test "${TESTS_OUTCOME}" = success',
        'test "${SMOKE_OUTCOME}" = success',
    ):
        assert required in text[enforce:publish]


def test_position_exit_actions_artifacts_are_failure_only_diagnostics() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count(UPLOAD_ACTION) == 2
    for marker in (
        "      - name: Upload fast failure diagnostics",
        "      - name: Upload full-release failure diagnostics",
    ):
        start = text.index(marker)
        block = text[start : start + 900]
        assert "if: ${{ failure() || cancelled() }}" in block
        assert "continue-on-error: true" in block
        assert UPLOAD_ACTION in block
        assert "retention-days:" not in block


def test_position_exit_quality_security_package_and_replay_gates_remain_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        'python-version: ["3.12", "3.13"]',
        "coverage report --fail-under=92",
        "Public import and entry-point smoke",
        "Full module suite",
        "Security and SBOM",
        "pip-audit --local --format json",
        "Build, verify and clean-install distributions",
        "Installed-sdist tests and wheel rebuild",
        "Deterministic replay and benchmark evidence",
        "cmp \\",
        "benchmark-restart.json",
        "Record exact-head evidence",
        "report-push-result:",
    ):
        assert required in text


def test_position_exit_cleanup_covers_evidence_and_temp_environments() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "Cleanup fast evidence" in text
    assert "rm -rf modules/position_exit_management/ci-fast" in text
    assert "Cleanup full-release evidence" in text
    for required in (
        "modules/position_exit_management/ci-evidence",
        "core-dist module-dist rebuilt-dist sdist-unpacked",
        '"${RUNNER_TEMP}/position-exit-clean-wheel"',
        '"${RUNNER_TEMP}/position-exit-sdist"',
    ):
        assert required in text
