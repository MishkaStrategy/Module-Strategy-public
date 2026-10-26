from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "trading-runner-ci.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_trading_runner_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: trading-runner" in text
    assert "pyproject.toml" in text
    assert "modules/trade_plan_router/pyproject.toml" in text
    assert "modules/paper_execution/pyproject.toml" in text
    assert "applications/trading_runner/pyproject.toml" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text


def test_trading_runner_checkout_is_shallow_and_read_only() -> None:
    text = _workflow_text()

    assert "clean: true" in text
    assert "fetch-depth: 1" in text
    assert "persist-credentials: false" in text
    assert "show-progress: false" in text


def test_trading_runner_preserves_component_quality_gates() -> None:
    text = _workflow_text()

    expected_fragments = (
        "python -m compileall -q applications/trading_runner/src",
        "ruff format --check applications/trading_runner/src",
        "ruff check applications/trading_runner/src",
        "mypy --strict applications/trading_runner/src",
        "pytest -q applications/trading_runner/tests",
        "(cd applications/trading_runner && python -m build)",
        "Collect clean wheel import diagnostics",
        "import modular_core, modular_trading_runner",
        'forbidden = ("LiveExecutionAdapter", "private_api_key", "production_trading")',
        "Trading Runner component gate passed",
    )
    for fragment in expected_fragments:
        assert fragment in text


def test_trading_runner_preserves_fail_closed_final_enforcement() -> None:
    text = _workflow_text()

    assert "if: always()" in text
    assert 'for path in sorted(report_dir.glob("*.exit"))' in text
    assert 'if code != "0":' in text
    assert "Trading Runner component gate failed" in text
