from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _core_workflow() -> str:
    return CORE_WORKFLOW.read_text(encoding="utf-8")


def test_core_ci_upload_is_failure_only_nonblocking_and_last() -> None:
    workflow = _core_workflow()

    upload_marker = "      - name: Upload Core CI diagnostics"
    assert upload_marker in workflow
    upload_start = workflow.index(upload_marker)
    upload = workflow[upload_start:]

    assert "        if: failure()" in upload
    assert "        continue-on-error: true" in upload
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in upload
    assert "name: core-ci-py${{ matrix.python-version }}-${{ github.run_attempt }}" in upload
    assert "if: always()" not in upload

    required_before_upload = (
        "      - name: Compile Core and root tests",
        "      - name: Ruff lint and format",
        "      - name: Focused strict MyPy",
        "      - name: Run root suites in isolated pytest processes",
        "      - name: Build clean wheel",
        "      - name: Verify installed wheel",
    )
    for marker in required_before_upload:
        assert marker in workflow
        assert workflow.index(marker) < upload_start

    assert workflow.count("actions/upload-artifact@") == 1


def test_no_repository_acceptance_surface_consumes_core_ci_artifact_name() -> None:
    search_roots = (
        ROOT / ".github" / "workflows",
        ROOT / "scripts",
        ROOT / "docs" / "integration",
    )
    matches: list[str] = []
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for path in search_root.rglob("*"):
            if not path.is_file() or path == CORE_WORKFLOW:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "core-ci-py" in text:
                matches.append(str(path.relative_to(ROOT)))

    assert matches == [], f"unexpected Core CI artifact consumers: {matches}"


def test_core_ci_acceptance_remains_status_and_quality_gate_based() -> None:
    checkpoint = (
        ROOT / "docs" / "integration" / "FULL_RC_ACCEPTANCE_CHECKPOINT_2026-08-04.md"
    ).read_text(encoding="utf-8")

    assert "Fast checks, Core CI and Backtest Engine on supported Python versions" in checkpoint
    assert "core-ci-py" not in checkpoint
