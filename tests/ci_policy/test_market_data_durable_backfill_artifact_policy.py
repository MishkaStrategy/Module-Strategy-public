from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/market-data-durable-backfill-ci.yml")
UPLOAD_ACTION = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def test_durable_backfill_upload_is_failure_cancelled_nonblocking_diagnostics() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count(UPLOAD_ACTION) == 1
    marker = "- name: Upload focused evidence"
    assert marker in text
    diagnostic = text[text.index(marker) :]
    assert "if: ${{ failure() || cancelled() }}" in diagnostic
    assert "continue-on-error: true" in diagnostic
    assert UPLOAD_ACTION in diagnostic
    assert "if-no-files-found: warn" in diagnostic
    assert "retention-days:" not in text
    assert text.index("Run durable backfill and parity tests") < text.index(marker)


def test_durable_backfill_quality_gates_remain_blocking_before_diagnostics() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    diagnostic = text.index("- name: Upload focused evidence")

    for gate in (
        "Compile durable backfill slice",
        "Ruff focused lint",
        "Ruff focused format",
        "Strict MyPy application",
        "Validate parity matrix",
        "Run durable backfill and parity tests",
    ):
        assert text.index(gate) < diagnostic
