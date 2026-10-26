from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "session-range-ci.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_session_range_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: session-range" in text
    assert "pyproject.toml" in text
    assert "modules/session_range/pyproject.toml" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text


def test_session_range_checkout_preserves_history_but_is_read_only() -> None:
    text = _workflow_text()

    assert "fetch-depth: 0" in text
    assert "clean: true" in text
    assert "persist-credentials: false" in text
    assert "show-progress: false" in text


def test_session_range_preserves_quality_and_package_gates() -> None:
    text = _workflow_text()

    required = (
        "ruff check modules/session_range/src",
        "ruff format --check modules/session_range/src",
        "mypy --strict modules/session_range/src/modular_session_range",
        "coverage report --fail-under=96",
        "tests/test_core_contract_freeze.py",
        "tests/test_core_module_integration_harness.py",
        "python -m build --wheel --outdir dist/session-range-ci/core .",
        "python -m build --wheel --sdist",
        "twine check dist/session-range-ci/core/*",
        "Clean non-editable install and entry-point smoke",
        'assert version("modular-strategy-core") == "0.4.2"',
        'assert version("modular-session-range") == "0.2.0"',
        'assert entries["session_range"].load() is SessionRangeModule',
    )
    for fragment in required:
        assert fragment in text
    assert "retention-days:" not in text


def test_session_range_preserves_matrix_and_concurrency() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.12", "3.13"]' in text
    assert "fail-fast: false" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 90" in text
    assert "runs-on: [self-hosted, fast]" in text
