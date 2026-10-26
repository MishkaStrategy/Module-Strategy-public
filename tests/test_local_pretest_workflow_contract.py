from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "local-pretest-tooling-ci.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_local_pretest_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: local-pretest-tooling" in text
    assert "cache-dependency-glob: .github/workflows/local-pretest-tooling-ci.yml" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text


def test_local_pretest_checkout_is_shallow_and_read_only() -> None:
    text = _workflow_text()

    assert "clean: true" in text
    assert "fetch-depth: 1" in text
    assert "persist-credentials: false" in text
    assert "show-progress: false" in text


def test_local_pretest_preserves_all_quality_gates() -> None:
    text = _workflow_text()

    required = (
        "uv pip install pytest ruff",
        "python -m compileall -q",
        "bash -n scripts/run_local_test_suite.sh",
        "ruff check --output-format=concise",
        "ruff format --check --diff",
        "Upload format failure diagnostics",
        'test "$(cat format-${{ matrix.python-version }}.status)" = "0"',
        "python -m pytest -q tests/local_pretest",
        "python scripts/local_pretest.py --help",
        "if-no-files-found: error",
    )
    for fragment in required:
        assert fragment in text
    assert "retention-days:" not in text


def test_local_pretest_preserves_matrix_and_concurrency() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.12", "3.13"]' in text
    assert "fail-fast: false" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 20" in text
    assert "runs-on: [self-hosted, fast]" in text
