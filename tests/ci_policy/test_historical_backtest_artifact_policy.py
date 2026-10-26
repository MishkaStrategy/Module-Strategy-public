from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/heavy-backtest.yml")
DISPATCHER = Path(".github/workflows/full-rc-heavy-validation.yml")
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_historical_backtest_success_evidence_uses_exact_sha_release_publication() -> None:
    text = _workflow_text()

    assert "permissions:\n  contents: write" in text
    assert "ref: ${{ inputs.expected_sha }}" in text
    assert "EXPECTED_SHA: ${{ inputs.expected_sha }}" in text
    assert "Stage historical backtest release evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "id: evidence_publish" in text
    assert "source_path: reports/historical-backtest-release" in text
    assert "asset_name: historical-strategy-backtest.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert 'test "${{ steps.evidence_publish.outcome }}" = "success"' in text


def test_historical_backtest_actions_upload_is_failure_cancelled_diagnostic_only() -> None:
    text = _workflow_text()

    assert text.count(UPLOAD_ACTION) == 1
    assert "- name: Upload historical backtest evidence" not in text
    marker = "- name: Upload historical backtest failure diagnostics"
    assert marker in text
    diagnostic = text[text.index(marker) :]
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in text
    assert text.index("Enforce historical evidence publication gate") < text.index(marker)


def test_historical_backtest_dispatcher_preserves_write_permission_chain() -> None:
    text = DISPATCHER.read_text(encoding="utf-8")

    assert "permissions:\n  contents: write" in text
    historical_start = text.index("historical-strategy-backtest:")
    signal_start = text.index("signal-bot-heavy-audit:")
    block = text[historical_start:signal_start]
    assert "github.event.label.name == 'historical-backtest-validation'" in block
    assert "github.event.pull_request.base.ref == 'integration/modular-strategy-full-rc'" in block
    assert "permissions:\n      contents: write" in block
    assert "uses: ./.github/workflows/heavy-backtest.yml" in block
    assert "expected_sha: ${{ github.event.pull_request.head.sha }}" in block

    backtest = text[text.index("backtest-engine-heavy:") : historical_start]
    assert "permissions:\n      contents: read" in backtest

    signal = text[signal_start : text.index("registered-canonical-parity-dispatch:")]
    assert "permissions:\n      contents: write" in signal

    parity = text[text.index("registered-canonical-parity-dispatch:") :]
    assert "actions: write" in parity
    assert "contents: read" in parity
