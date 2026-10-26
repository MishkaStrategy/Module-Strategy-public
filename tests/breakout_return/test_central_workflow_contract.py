from __future__ import annotations

from pathlib import Path


def _workflow_path() -> Path:
    test_file = Path(__file__).resolve()
    for parent in test_file.parents:
        candidate = parent / ".github" / "workflows" / "breakout-return-hardening-ci.yml"
        if candidate.is_file():
            return candidate
    raise AssertionError("breakout-return-hardening-ci.yml was not found")


def test_central_repository_suites_are_isolated_and_fully_provisioned() -> None:
    text = _workflow_path().read_text(encoding="utf-8")

    assert "runs-on: [self-hosted, fast]" in text
    assert "-e '.[dev,backtest,signal-bot]'" in text
    assert "-e ./modules/entry_model" in text
    assert "-e './modules/position_exit_management[dev]'" in text
    assert "python -m pytest -q tests/test_*.py" in text
    assert "python -m pytest -q tests/backtest_engine" in text
    assert "python -m pytest -q tests/breakout_return" in text
    assert "python -m pytest -q tests/integration" in text
    assert "python -m pytest -q tests/signal_bot" in text
    assert "run: python -m pytest --junitxml=repository-junit.xml" not in text


def test_secret_scan_is_fail_closed_and_component_scoped() -> None:
    text = _workflow_path().read_text(encoding="utf-8")

    assert "detect-secrets scan \\" in text
    assert "src/strategy_modules/breakout_return \\" in text
    assert "tests/breakout_return \\" in text
    assert "benchmarks/breakout_return.py \\" in text
    assert "benchmarks/breakout_return_deep.py \\" in text
    assert "detect-secrets scan src tests benchmarks" not in text
    assert 'findings = json.load(handle).get("results", {})' in text
    assert "if findings:" in text
    assert 'raise SystemExit(f"detect-secrets findings: {sorted(findings)}")' in text
