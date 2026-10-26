from __future__ import annotations

from pathlib import Path


def _workflow_text() -> str:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "paper-execution-ci.yml"
    )
    return workflow.read_text(encoding="utf-8")


def test_paper_execution_uses_shared_cached_python_preflight() -> None:
    text = _workflow_text()

    assert "uses: ./.github/actions/python-runner-preflight" in text
    assert "role: fast" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert "cache-suffix: paper-execution" in text
    assert "modules/paper_execution/pyproject.toml" in text
    assert ".contract-sources/trade-plan-router/pyproject.toml" in text
    assert ".contract-sources/trade-plan-router/modules/trade_plan_router" in text
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text


def test_paper_execution_checkouts_are_shallow_and_read_only() -> None:
    text = _workflow_text()

    assert text.count("persist-credentials: false") == 2
    assert text.count("fetch-depth: 1") == 2
    assert text.count("show-progress: false") == 2
    assert "ref: f779e599b361a7b860506980de6b5ae8d0cd1f01" in text
    assert "path: .contract-sources/trade-plan-router" in text


def test_paper_execution_preserves_component_gate_and_evidence() -> None:
    text = _workflow_text()

    expected_fragments = (
        'uv pip install -e ".[dev]"',
        'uv pip install -e "./modules/paper_execution[dev]"',
        "./.contract-sources/trade-plan-router/modules/trade_plan_router",
        "uv pip install bandit build twine",
        "uv pip check",
        "python scripts/paper_execution_component_gate.py",
        "dist/paper_execution-component/*",
        "reports/paper_execution/*",
        "if-no-files-found: warn",
    )
    for fragment in expected_fragments:
        assert fragment in text
    assert "retention-days:" not in text


def test_paper_execution_preserves_matrix_and_concurrency() -> None:
    text = _workflow_text()

    assert 'python-version: ["3.12", "3.13"]' in text
    assert "fail-fast: false" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes: 90" in text
    assert "runs-on: [self-hosted, fast]" in text
