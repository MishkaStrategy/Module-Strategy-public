from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/signal-bot-heavy-audit.yml")
DISPATCHER = Path(".github/workflows/full-rc-heavy-validation.yml")
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_signal_bot_heavy_success_evidence_uses_exact_sha_release_publication() -> None:
    text = _workflow_text()

    assert "permissions:\n  contents: write" in text
    assert "ref: ${{ inputs.expected_sha }}" in text
    assert "EXPECTED_SHA: ${{ inputs.expected_sha }}" in text
    assert "Verify exact SHA and record environment" in text
    assert "Stage Signal Bot Heavy release evidence" in text
    assert "uses: ./.github/actions/publish-release-evidence" in text
    assert "id: evidence_publish" in text
    assert "source_path: reports/signal-bot-heavy-release" in text
    assert "asset_name: signal-bot-heavy-audit.tar.gz" in text
    assert "commit_sha: ${{ env.EXPECTED_SHA }}" in text
    assert "Enforce Signal Bot evidence publication gate" in text


def test_signal_bot_heavy_actions_upload_is_failure_cancelled_diagnostic_only() -> None:
    text = _workflow_text()

    assert text.count(UPLOAD_ACTION) == 1
    assert "- name: Upload exact Signal Bot Heavy evidence" not in text
    marker = "- name: Upload Signal Bot Heavy failure diagnostics"
    assert marker in text
    diagnostic = text[text.index(marker) :]
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in text
    assert text.index("Enforce Signal Bot evidence publication gate") < text.index(marker)


def test_signal_bot_heavy_dispatcher_preserves_write_permission_chain() -> None:
    text = DISPATCHER.read_text(encoding="utf-8")

    assert "branches: [main, integration/modular-strategy-full-rc]" in text
    assert "permissions:\n  contents: write" in text

    backtest_start = text.index("backtest-engine-heavy:")
    historical_start = text.index("historical-strategy-backtest:")
    signal_start = text.index("signal-bot-heavy-audit:")
    parity_start = text.index("registered-canonical-parity-dispatch:")

    backtest = text[backtest_start:historical_start]
    historical = text[historical_start:signal_start]
    signal = text[signal_start:parity_start]
    parity = text[parity_start:]

    assert "permissions:\n      contents: read" in backtest
    assert "permissions:\n      contents: write" in historical
    assert "github.event.label.name == 'signal-bot-heavy-validation'" in signal
    assert "github.event.pull_request.base.ref == 'integration/modular-strategy-full-rc'" in signal
    assert "permissions:\n      contents: write" in signal
    assert "uses: ./.github/workflows/signal-bot-heavy-audit.yml" in signal
    assert "expected_sha: ${{ github.event.pull_request.head.sha }}" in signal
    assert "actions: write" in parity
    assert "contents: read" in parity
