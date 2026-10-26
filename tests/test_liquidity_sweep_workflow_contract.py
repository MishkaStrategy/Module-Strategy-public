from __future__ import annotations

from pathlib import Path

import pytest


SETUP_UV_V7_SHA = "37802adc94f370d6bfd71619e3f0bf239e1f3b78"  # pragma: allowlist secret


def _workflow_text() -> str:
    test_file = Path(__file__).resolve()
    for parent in test_file.parents:
        candidate = parent / ".github" / "workflows" / "liquidity-sweep-hardening-ci.yml"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    pytest.skip("repository workflow is not present in the installed package test environment")


def test_workflow_uses_portable_managed_python_matrix() -> None:
    text = _workflow_text()

    assert "runs-on: [self-hosted, fast]" in text
    assert 'python-version: ["3.12", "3.13"]' in text
    assert f"uses: astral-sh/setup-uv@{SETUP_UV_V7_SHA}" in text
    assert 'version: "0.11.31"' in text
    assert "uses: astral-sh/setup-uv@v7" not in text
    assert "astral-sh/setup-uv@v6" not in text
    assert "actions/setup-python@v5" not in text
    assert "CHANGELOG.md" not in text


def test_workflow_preserves_focused_quality_and_recovery_gates() -> None:
    text = _workflow_text()

    assert "coverage run --branch --source=src/strategy_modules/liquidity_sweep" in text
    assert "coverage report --show-missing --fail-under=90" in text
    assert "Explicit causality, restart and lifecycle gate" in text
    assert "tests/test_liquidity_sweep_error_discovery.py" in text
    assert "tests/test_liquidity_sweep_state_capacity_edges.py" in text
    assert "Clean wheel, PEP 561 and checkpoint smoke" in text
    assert "Clean source-distribution suite" in text
    assert "discover_entry_points()" in text
    assert "capture_module_states" in text
    assert "restore_module_states" in text


def test_central_compatibility_is_provisioned_and_isolated() -> None:
    text = _workflow_text()

    assert 'echo "INTEGRATION_VENV=$RUNNER_TEMP/liquidity-sweep-integration-$suffix"' in text
    assert "Install isolated Stage1 compatibility environment" in text
    assert 'python -m venv "$INTEGRATION_VENV"' in text
    assert "\"$INTEGRATION_VENV/bin/python\" -m pip install -e '.[dev,backtest,signal-bot]'" in text
    assert (
        '"$INTEGRATION_VENV/bin/python" -m pip install -e '
        "'./modules/market_data_ingestion[dev,parquet]'"
    ) in text
    assert '"$INTEGRATION_VENV/bin/python" -m pip install -e ./research/experiment_runner' in text
    assert (
        "\"$INTEGRATION_VENV/bin/python\" -m pip install -e './applications/system_acceptance[dev]'"
    ) in text
    assert '"$INTEGRATION_VENV/bin/python" -m pytest -q tests/test_*.py' in text
    assert '"$INTEGRATION_VENV/bin/python" -m pytest -q tests/backtest_engine' in text
    assert '"$INTEGRATION_VENV/bin/python" -m pytest -q tests/breakout_return' in text
    assert '"$INTEGRATION_VENV/bin/python" -m pytest -q tests/integration' in text
    assert '"$INTEGRATION_VENV/bin/python" -m pytest -q tests/signal_bot' in text
    assert 'rm -rf "$VENV" "$WHEEL_VENV" "$SDIST_VENV" "$INTEGRATION_VENV"' in text
    assert '"$INTEGRATION_VENV/bin/pip-audit"' not in text
    assert "run: python -m pytest --junitxml=repository-junit.xml" not in text


def test_security_benchmark_and_restart_stress_remain_fail_closed() -> None:
    text = _workflow_text()

    assert "bandit -q -r" in text
    assert "detect-secrets scan \\" in text
    assert 'findings = json.load(handle).get("results", {})' in text
    assert "if findings:" in text
    assert "pip-audit --format json" in text
    assert "cyclonedx-py environment" in text
    assert "Deterministic benchmark smoke" in text
    assert "One-million-event segmented restart stress" in text
    assert 'assert result["total_events"] == 1_000_000' in text
    assert 'assert result["next_index"] == 1_000_000' in text
