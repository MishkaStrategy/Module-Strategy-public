from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/position-exit-management-ci.yml"


def test_workflow_has_exact_head_resource_and_quality_guards() -> None:
    text = WORKFLOW.read_text()
    required = (
        "runs-on: [self-hosted, fast]",
        "workflow_dispatch:",
        "issues: write",
        "cancel-in-progress: true",
        "timeout-minutes: 20",
        "timeout-minutes: 180",
        "github.event_name == 'push'",
        "TARGET_SHA:",
        "ref: ${{ env.TARGET_SHA }}",
        'test "$(git rev-parse HEAD)" = "${TARGET_SHA}"',
        "uses: ./.github/actions/python-runner-preflight",
        "cache-suffix: position-exit-fast",
        "cache-suffix: position-exit-full",
        "persist-credentials: false",
        "ruff check --output-format=concise",
        "ruff format --check",
        "coverage report --fail-under=92",
        "detect-secrets scan",
        "detect-secrets found candidates",
        "pip-audit --local --format json",
        "cyclonedx-py environment --output-format JSON",
        "verify_release_artifacts.py",
        "Installed-sdist tests and wheel rebuild",
        'pip install --no-deps "${SDIST}"',
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "report-push-result:",
        "Enforce fast-quality results",
        "Enforce successful aggregate result",
    )
    for token in required:
        assert token in text

    fast_job = text.index("  fast-quality:")
    heavy_job = text.index("  full-release-gates:")
    report_job = text.index("  report-push-result:")
    assert fast_job < heavy_job < report_job

    fast_section = text[fast_job:heavy_job]
    heavy_section = text[heavy_job:report_job]
    assert "runs-on: [self-hosted, fast]" in fast_section
    assert "runs-on: [self-hosted, fast]" in heavy_section
    assert "role: fast" in fast_section
    assert "role: fast" in heavy_section
    assert "python-version: ${{ matrix.python-version }}" in fast_section
    assert 'python-version: "3.13"' in heavy_section
    assert "Linux, X64" not in fast_section
    assert "Linux, X64" not in heavy_section
    assert "macOS, ARM64" not in fast_section
    assert "macOS, ARM64" not in heavy_section

    lowered = text.lower()
    assert "uses: astral-sh/setup-uv@" not in text
    assert "enable-cache: false" not in text
    assert "retention-days:" not in text
    assert "ubuntu-latest" not in lowered
    assert "macos-latest" not in lowered
    assert "git push" not in lowered
    assert "force: true" not in lowered
