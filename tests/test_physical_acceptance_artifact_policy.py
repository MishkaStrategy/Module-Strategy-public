from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "physical-mac-acceptance.yml"
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
RELEASE_ACTION = "uses: ./.github/actions/publish-release-evidence"


def _step(text: str, marker: str, next_marker: str | None = None) -> str:
    start = text.index(marker)
    if next_marker is None:
        return text[start:]
    end = text.index(next_marker, start + len(marker))
    return text[start:end]


def test_physical_acceptance_success_evidence_is_release_backed() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "permissions:\n  contents: write" in text

    publications = (
        (
            "      - name: Publish immutable historical acceptance evidence",
            "      - name: Enforce historical evidence publication gate",
            "asset_name: physical-mac-historical.tar.gz",
        ),
        (
            "      - name: Publish immutable backend acceptance evidence",
            "      - name: Enforce backend evidence publication gate",
            "asset_name: physical-mac-backend.tar.gz",
        ),
        (
            "      - name: Publish immutable Dashboard/PAPER evidence",
            "      - name: Enforce Dashboard/PAPER evidence publication gate",
            "asset_name: physical-mac-dashboard-paper.tar.gz",
        ),
        (
            "      - name: Publish immutable final acceptance summary",
            "      - name: Enforce final evidence publication gate",
            "asset_name: physical-mac-final.tar.gz",
        ),
    )

    for marker, next_marker, asset in publications:
        block = _step(text, marker, next_marker)
        assert "if: success()" in block
        assert RELEASE_ACTION in block
        assert asset in block
        assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in block
        assert 'draft: "true"' in block
        assert UPLOAD_ACTION not in block
        assert "continue-on-error:" not in block


def test_physical_acceptance_actions_artifacts_are_one_day_failure_diagnostics() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    diagnostics = (
        ("      - name: Upload historical failure diagnostics", "  control-plane-recovery:"),
        ("      - name: Upload backend failure diagnostics", "  dashboard-paper:"),
        ("      - name: Upload Dashboard/PAPER failure diagnostics", "  final-summary:"),
        ("      - name: Upload final-summary failure diagnostics", None),
    )

    for marker, next_marker in diagnostics:
        block = _step(text, marker, next_marker)
        assert "if: ${{ failure() || cancelled() }}" in block
        assert "continue-on-error: true" in block
        assert UPLOAD_ACTION in block
        assert "retention-days: 1" in block
        assert RELEASE_ACTION not in block

    assert text.count(UPLOAD_ACTION) == 4
    assert "Upload sanitized historical acceptance evidence" not in text
    assert "Upload sanitized backend acceptance evidence" not in text
    assert "Upload sanitized Dashboard/PAPER evidence" not in text
    assert "Upload final acceptance summary" not in text


def test_final_summary_fails_closed_before_success_publication() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    final_job = _step(text, "  final-summary:")

    gate_index = final_job.index("      - name: Enforce automated acceptance result")
    publication_index = final_job.index("      - name: Publish immutable final acceptance summary")
    diagnostic_index = final_job.index("      - name: Upload final-summary failure diagnostics")

    assert gate_index < publication_index < diagnostic_index
    gate = _step(
        final_job,
        "      - name: Enforce automated acceptance result",
        "      - name: Publish immutable final acceptance summary",
    )
    assert 'test "$HISTORICAL_RESULT" = success' in gate
    assert 'test "$BACKEND_RESULT" = success' in gate
    assert 'test "$DASHBOARD_PAPER_RESULT" = success' in gate
