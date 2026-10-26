from __future__ import annotations

from pathlib import Path

SETUP_UV_SHA = "37802adc94f370d6bfd71619e3f0bf239e1f3b78"  # pragma: allowlist secret


def _workflow_text() -> str:
    test_file = Path(__file__).resolve()
    for parent in test_file.parents:
        candidate = parent / ".github" / "workflows" / "wave-analysis.yml"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError("wave-analysis.yml was not found")


def test_workflow_uses_portable_managed_python_matrix() -> None:
    text = _workflow_text()

    assert "runs-on: [self-hosted, fast]" in text
    assert 'python-version: ["3.12", "3.13"]' in text
    assert f"uses: astral-sh/setup-uv@{SETUP_UV_SHA}" in text
    assert "uses: astral-sh/setup-uv@v7" not in text
    assert 'version: "0.11.31"' in text
    assert "actions/setup-python@v5" not in text


def test_workflow_preserves_quality_and_causality_gates() -> None:
    text = _workflow_text()

    assert "coverage run --branch --source=wave_analysis" in text
    assert "coverage report --fail-under=95" in text
    assert "Explicit no-look-ahead and lifecycle gate" in text
    assert "test_segment_isolation.py" in text
    assert "test_feature_lifecycle.py" in text
    assert "python -m twine check dist/wave/*" in text
    assert "wave-analysis-clean-wheel=ok" in text


def test_clean_wheel_smoke_discovers_entry_points_before_named_load() -> None:
    text = _workflow_text()

    discovery = text.index("discovered = registry.discover_entry_points()")
    named_load = text.index('module = registry.load_named("wave_analysis")')
    assert discovery < named_load
    assert 'assert "wave_analysis" in discovered' in text
    assert "MarketView((), {}, 0, 0.0, 0.0, None, ())" in text
    assert "MarketView((), {}, -1," not in text


def test_security_gate_retains_all_diagnostics_and_remains_fail_closed() -> None:
    text = _workflow_text()

    assert "wave-analysis-bandit-advisory.json" in text
    assert "bandit_advisory_status=$?" in text
    assert "--skip B101" in text
    assert "--skip B101," not in text
    assert "-f json -o wave-analysis-bandit.json" in text
    assert "bandit_status=$?" in text
    assert "secret_scan_status=$?" in text
    assert "secret_findings_status=$?" in text
    assert "audit_status=$?" in text
    assert "sbom_status=$?" in text
    assert "wave-analysis-security-status.txt" in text
    assert "bandit_advisory_status != 0" not in text
    assert "bandit_status != 0" in text
    assert "secret_scan_status != 0" in text
    assert "secret_findings_status != 0" in text
    assert "audit_status != 0" in text
    assert "sbom_status != 0" in text


def test_workflow_is_fail_closed_and_centrally_compatible() -> None:
    text = _workflow_text()

    assert "uv pip install -e '.[dev,backtest,signal-bot]'" in text
    assert "uv pip install -e './applications/trading_runner[dev]'" in text
    assert "python -m pytest -q tests/test_*.py" in text
    assert "python -m pytest -q tests/backtest_engine" in text
    assert "python -m pytest -q tests/breakout_return" in text
    assert "python -m pytest -q tests/integration" in text
    assert "python -m pytest -q tests/signal_bot" in text
    assert "detect-secrets scan \\" in text
    assert "modules/wave_analysis/src \\" in text
    assert "modules/wave_analysis/tests \\" in text
    assert 'findings = json.load(handle).get("results", {})' in text
    assert "if findings:" in text
