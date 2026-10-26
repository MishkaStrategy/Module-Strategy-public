from pathlib import Path


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_dispatcher_is_the_only_research_opt_in_pr_surface() -> None:
    dispatcher = _read("research-runner-validation.yml")
    ordinary = _read("research-runner-ci.yml")

    assert "  pull_request:\n" in dispatcher
    assert "types: [labeled, synchronize]" in dispatcher
    for name in (
        "research-runner-full.yml",
        "research-runner-heavy.yml",
        "research-runner-release.yml",
        "research-runner-security.yml",
    ):
        assert f"uses: ./.github/workflows/{name}" in dispatcher

    ordinary_trigger = ordinary.split("\npermissions:\n", 1)[0]
    assert "types: [opened, reopened, synchronize, ready_for_review]" in ordinary_trigger
    assert "labeled" not in ordinary_trigger
    assert "full-integration" not in ordinary


def test_dispatcher_preserves_each_label_across_synchronize() -> None:
    dispatcher = _read("research-runner-validation.yml")

    for label in (
        "full-integration",
        "heavy-validation",
        "release-validation",
        "security-validation",
    ):
        assert f"github.event.label.name == '{label}'" in dispatcher
        assert (
            "github.event.action == 'synchronize' && "
            f"contains(github.event.pull_request.labels.*.name, '{label}')" in dispatcher
        )


def test_specialized_research_workflows_are_reusable() -> None:
    labels = {
        "research-runner-full.yml": "full-integration",
        "research-runner-heavy.yml": "heavy-validation",
        "research-runner-release.yml": "release-validation",
        "research-runner-security.yml": "security-validation",
    }
    for name, label in labels.items():
        text = _read(name)
        trigger = text.split("\npermissions:\n", 1)[0]
        assert "  workflow_call:" in trigger
        assert "  pull_request:" not in trigger
        assert label not in text


def test_ordinary_ready_and_manual_full_validation_use_reusable_workflow() -> None:
    ordinary = _read("research-runner-ci.yml")

    assert "  workflow_dispatch:\n" in ordinary
    assert "github.event_name == 'workflow_dispatch'" in ordinary
    assert "github.event.action == 'ready_for_review'" in ordinary
    assert "uses: ./.github/workflows/research-runner-full.yml" in ordinary
    assert "Research Runner full / Python" not in ordinary


def test_full_reusable_preserves_quality_and_release_evidence_contract() -> None:
    full = _read("research-runner-full.yml")

    for required in (
        'python-version: ["3.12", "3.13"]',
        "Checkout Release evidence publisher",
        "Verify exact Research Runner candidate",
        "Checkout authoritative Backtest Engine branch",
        "Checkout authoritative Results Aggregator branch",
        "coverage report --fail-under=90",
        "Verify authoritative Backtest Engine suite",
        "Build and smoke-test wheel",
        "Verify reproducible Research Runner wheel",
        "Sanitize and verify full evidence privacy",
        "Stage bounded Research Runner Full release evidence",
        "Publish exact Research Runner Full evidence",
        "Enforce Research Runner Full publication gate",
        "Upload Research Runner Full failure diagnostics",
    ):
        assert required in full

    assert "permissions:\n  contents: write" in full
    assert "uses: ./.github/actions/publish-release-evidence" in full
    assert "asset_name: research-runner-full-py${{ matrix.python-version }}.tar.gz" in full
    assert "commit_sha: ${{ env.EXPECTED_HEAD }}" in full
    assert 'draft: "true"' in full
    assert "core_wheels=(dist/core/*.whl)" in full
    assert "runner_wheels=(dist/research_runner/*.whl)" in full
    assert "report_entries=(reports/*)" in full
    assert 'cp "${core_wheels[@]}" "$bundle/dist/core/"' in full
    assert 'cp "${runner_wheels[@]}" "$bundle/dist/research_runner/"' in full
    assert "steps.evidence_stage.outcome == 'success'" in full
    assert 'test "${{ steps.evidence_stage.outcome }}" = success' in full
    assert 'test "${{ steps.evidence_publish.outcome }}" = success' in full

    diagnostics = full.split("      - name: Upload Research Runner Full failure diagnostics", 1)[1]
    assert "failure() || cancelled()" in diagnostics
    assert "continue-on-error: true" in diagnostics
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in diagnostics
    assert "retention-days:" not in diagnostics
    assert "      - name: Upload full evidence" not in full


def test_full_release_publisher_permission_chain_is_explicit() -> None:
    dispatcher = _read("research-runner-validation.yml")
    ordinary = _read("research-runner-ci.yml")
    full = _read("research-runner-full.yml")

    assert "permissions:\n  contents: write" in dispatcher
    assert "permissions:\n  contents: write" in ordinary
    assert "permissions:\n  contents: write" in full


def test_heavy_reusable_workflow_keeps_scale_input() -> None:
    heavy = _read("research-runner-heavy.yml")
    trigger = heavy.split("\npermissions:\n", 1)[0]

    assert "  workflow_call:\n" in trigger
    assert "      scale:\n" in trigger
    assert '        default: "5"\n' in trigger
    assert "        type: string\n" in trigger
