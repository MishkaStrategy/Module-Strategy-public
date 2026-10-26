from __future__ import annotations

from pathlib import Path


def _workflow_path() -> Path:
    test_file = Path(__file__).resolve()
    for parent in test_file.parents:
        candidate = parent / ".github" / "workflows" / "protection-target-ci.yml"
        if candidate.is_file():
            return candidate
    raise AssertionError("protection-target-ci.yml was not found in the module or monorepo root")


def test_self_hosted_workflow_security_is_fail_closed() -> None:
    text = _workflow_path().read_text(encoding="utf-8")

    assert "runs-on: [self-hosted, fast]" in text
    assert "runs-on: [self-hosted, macOS, ARM64]" not in text
    assert "cancel-in-progress: true" in text
    assert 'matrix:\n        python-version: ["3.12", "3.13"]' in text
    assert "Provision exact Python" in text
    assert '"$UV_BOOTSTRAP/bin/uv" python install "$version"' in text
    assert '"$PYTHON_BIN" -m venv "$VENV"' in text
    assert '"pytest>=9.0.3"' in text
    assert "Monorepo compatibility" in text
    assert "-e '.[dev,backtest,signal-bot]'" in text
    assert "-e ./modules/entry_model" in text
    assert "-e './modules/position_exit_management[dev]'" in text
    assert '"$VENV/bin/python" -m pytest -q tests/test_*.py' in text
    assert '"$VENV/bin/python" -m pytest -q tests/backtest_engine' in text
    assert '"$VENV/bin/python" -m pytest -q tests/breakout_return' in text
    assert '"$VENV/bin/python" -m pytest -q tests/integration' in text
    assert '"$VENV/bin/python" -m pytest -q tests/signal_bot' in text
    assert '"$VENV/bin/python" -m pytest -q tests \\' not in text
    assert "scripts/integration_smoke.py" in text
    assert '"$VENV/bin/detect-secrets" scan' in text
    assert 'report.get("results", {})' in text
    assert "if findings:" in text
    assert 'raise SystemExit(f"detect-secrets findings present: {findings}")' in text
    assert '"$VENV/bin/pip-audit" --local' in text
    assert "retention-days:" not in text
