from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "market-regime-ci.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_market_regime_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: market-regime" in text
    assert "pyproject.toml" in text
    assert "modules/market_regime/pyproject.toml" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text
    assert "UV_PYTHON_INSTALL_DIR=" not in text


def test_market_regime_checkout_is_shallow_and_read_only() -> None:
    text = _workflow_text()

    assert "clean: true" in text
    assert "fetch-depth: 1" in text
    assert "persist-credentials: false" in text
    assert "show-progress: false" in text


def test_market_regime_preserves_quality_and_package_gates() -> None:
    text = _workflow_text()

    required = (
        "python -m compileall -q",
        "ruff check",
        "ruff format --check",
        "mypy --strict src/market_regime",
        "python -m pytest modules/market_regime/tests",
        '-m "not slow"',
        "Public import and entry-point smoke",
        'assert points["market_regime"].load() is MarketRegimeModule',
        "python -m build --no-isolation modules/market_regime",
        "--wheel --outdir dist/market-regime",
        "if-no-files-found: warn",
    )
    for fragment in required:
        assert fragment in text
    assert "retention-days:" not in text


def test_market_regime_preserves_matrix_concurrency_and_cleanup() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.12", "3.13"]' in text
    assert "fail-fast: false" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 20" in text
    assert "runs-on: [self-hosted, fast]" in text
    assert "rm -rf reports dist .pytest_cache .mypy_cache .ruff_cache" in text
    assert "modules/market_regime/.pytest_cache" in text
