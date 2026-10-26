from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "backtest-engine-heavy.yml"
SOURCE_SHA = "408d09c05435292d6995060a5d2fd0d4feb0f16e"
REQUEST_FINGERPRINT = "203f997e45e9f35931b994e6168d11f2ff2704f9cf185cc33d3997e6dca29848"
DATASET_FINGERPRINT = "8cade21615ef460a133925b9f5dcef5ab5e32bddee77c207c7d4be35da7fee80"
RUNTIME_FINGERPRINT = "e84520e50af54e65dc6b42558fe04182146e088eb7ad7c66066fb72a726e55e9"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    assert start in text
    assert end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def test_phase_b_binds_exact_producer_and_frozen_request_identities() -> None:
    text = _workflow()

    assert f"EXPECTED_HEAD: {SOURCE_SHA}" in text
    assert f"REQUEST_FINGERPRINT: {REQUEST_FINGERPRINT}" in text
    assert f"DATASET_FINGERPRINT: {DATASET_FINGERPRINT}" in text
    assert f"RUNTIME_SURFACE_FINGERPRINT: {RUNTIME_FINGERPRINT}" in text
    assert "timeout-minutes: 1560" in text
    assert '"maximum_duration_seconds": 86400' in text
    assert '"maximum_memory_bytes": 8589934592' in text
    assert '"maximum_cpu_workers": 1' in text
    assert '"maximum_parallel_tasks": 1' in text


def test_search_execution_defers_failed_verdict_until_after_handoff() -> None:
    text = _workflow()
    execution = _section(
        text,
        "      - name: Execute exactly one separately authorized SEARCH\n",
        "      - name: Validate SEARCH terminal envelope\n",
    )
    handoff = _section(
        text,
        "      - name: Prepare sanitized integrity-checked evidence outputs\n",
        "      - name: Restore SEARCH execution verdict\n",
    )
    restore = text.split("      - name: Restore SEARCH execution verdict\n", 1)[1]

    assert "id: search_execution" in execution
    assert "continue-on-error: true" in execution
    assert "steps.search_execution.outcome" in text
    assert "id: evidence_handoff" in handoff
    assert "steps.terminal_boundary.outcome == 'success'" in handoff
    assert "steps.evidence_assembly.outcome == 'success'" in handoff

    assert text.index("id: search_execution") < text.index("id: evidence_handoff")
    assert text.index("id: evidence_handoff") < text.index(
        "      - name: Restore SEARCH execution verdict"
    )
    assert 'test "$TERMINAL_BOUNDARY_OUTCOME" = "success"' in restore
    assert 'test "$EVIDENCE_ASSEMBLY_OUTCOME" = "success"' in restore
    assert 'test "$EVIDENCE_HANDOFF_OUTCOME" = "success"' in restore
    assert 'test "$SEARCH_EXECUTION_OUTCOME" = "success"' in restore


def test_terminal_boundary_distinguishes_success_and_bounded_failure_envelopes() -> None:
    text = _workflow()
    boundary = _section(
        text,
        "      - name: Validate SEARCH terminal envelope\n",
        "      - name: Assemble sanitized SEARCH evidence\n",
    )

    assert "if: ${{ inputs.execute_search && always() }}" in boundary
    assert "id: terminal_boundary" in boundary
    assert 'if payload.get("schema_version") != 1:' in boundary
    assert "if len(raw) > 4096:" in boundary
    assert '"technical_failure"' in boundary
    assert '"technical_interrupted"' in boundary
    assert '"ordinary_exception"' in boundary
    assert '"omitted_for_publication_safety"' in boundary
    assert 'forbidden_fragments = ("finalist", "candidate", "pareto")' in boundary
    assert 'payload.get("runtime_state_verified") is not False' in boundary

    success_branch = boundary.split('if outcome == "success":', 1)[1].split(
        'elif outcome == "failure":', 1
    )[0]
    failure_branch = boundary.split('elif outcome == "failure":', 1)[1]

    assert 'payload.get("next_stage") != "walk_forward"' in success_branch
    assert 'payload.get("finalists", [])' in success_branch
    assert '"breakout_return", "fvg_rt", "liquidity_sweep"' in success_branch

    assert 'payload.get("finalists", [])' not in failure_branch
    assert "reject_success_shape(payload)" in failure_branch


def test_failure_handoff_is_minimal_sanitized_and_zero_downstream_authority() -> None:
    text = _workflow()
    assembly = _section(
        text,
        "      - name: Assemble sanitized SEARCH evidence\n",
        "      - name: Prepare sanitized integrity-checked evidence outputs\n",
    )

    assert "id: evidence_assembly" in assembly
    assert "steps.terminal_boundary.outcome == 'success'" in assembly
    assert 'cp "$RUNNER_TEMP/ms5a-governed-search-summary.json"' in assembly
    assert 'cp "$RUNNER_TEMP/ms5a-reusable-host-maintenance.json"' in assembly
    assert 'if [[ "$SEARCH_EXECUTION_OUTCOME" == "success" ]]; then' in assembly
    assert 'cp "$RUNTIME_ROOT/search-authority.json"' in assembly
    assert 'cp "$RUNTIME_ROOT/frozen-request-meta.json"' in assembly
    assert '"search_failure"' in assembly
    assert '"downstream_authority": "none"' in assembly
    assert 'if execution_outcome == "success":' in assembly
    assert 'payload["runtime_locator"]' in assembly
    assert "sanitize_ci_evidence.py" in assembly
    assert "SHA256SUMS.txt" in assembly


def test_phase_b_does_not_add_dispatch_or_downstream_execution_surface() -> None:
    text = _workflow()

    # The historical heavy-audit manual entrypoint remains singular; Phase B adds none.
    assert text.count("  workflow_dispatch:") == 1
    assert "repository_dispatch:" not in text
    assert "issue_comment:" not in text
    assert "walk_forward_executed" in text
    assert "holdout_opened" in text
    assert "live_trading_authorized" in text
