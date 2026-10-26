from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "results-aggregator-fast.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_results_aggregator_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: results-aggregator" in text
    assert "pyproject.toml" in text
    assert "modules/results_aggregator/pyproject.toml" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text


def test_results_aggregator_checkout_is_shallow_and_read_only() -> None:
    text = _workflow_text()

    assert "clean: true" in text
    assert "fetch-depth: 1" in text
    assert "persist-credentials: false" in text
    assert "show-progress: false" in text


def test_results_aggregator_preserves_quality_and_package_gates() -> None:
    text = _workflow_text()

    required = (
        "ruff check --output-format=concise",
        "mypy modules/results_aggregator/src/modular_results_registry",
        "python -m pytest -q modules/results_aggregator/tests",
        "coverage run --branch --source=modular_results_registry",
        "coverage report --fail-under=90",
        "python modules/results_aggregator/tools/security_scan.py",
        "python -m pip wheel ./modules/results_aggregator",
        "--no-deps --no-build-isolation",
        "Clean wheel smoke",
        'assert package.__version__ == "0.1.0.dev0"',
        "assert len(list_schema_names()) == 17",
        "if-no-files-found: error",
    )
    for fragment in required:
        assert fragment in text
    assert "retention-days:" not in text


def test_results_aggregator_preserves_matrix_and_concurrency() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.12", "3.13"]' in text
    assert "fail-fast: false" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 60" in text
    assert "runs-on: [self-hosted, fast]" in text
