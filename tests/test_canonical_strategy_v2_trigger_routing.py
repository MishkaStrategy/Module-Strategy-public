from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "canonical-strategy-v2-backtest.yml"


def _pull_request_paths() -> tuple[str, ...]:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("  pull_request:")
    paths_start = lines.index("    paths:", start)
    paths: list[str] = []
    for line in lines[paths_start + 1 :]:
        if not line.startswith("      - "):
            break
        paths.append(line.removeprefix("      - ").strip().strip('"'))
    return tuple(paths)


def test_canonical_backtest_excludes_shared_ci_policy_scripts() -> None:
    paths = set(_pull_request_paths())

    assert "scripts/audit_runner_routing.py" not in paths
    assert "scripts/install_actionlint.sh" not in paths
    assert "scripts/runner_preflight.sh" not in paths


def test_canonical_backtest_retains_real_execution_and_research_dependencies() -> None:
    paths = set(_pull_request_paths())

    assert {
        ".github/actions/python-runner-preflight/**",
        ".github/workflows/canonical-strategy-v2-backtest.yml",
        "config/test_suites.toml",
        "reports/strategy-governance-wave2f/handoff-to-stream-a.json",
        "research/experiment_runner/benchmarks/wave2a_canonical_strategy.py",
        "research/experiment_runner/src/**",
        "research/strategy_audit_validation/benchmarks/**",
        "research/strategy_audit_validation/src/strategy_audit_validation/canonical_runtime.py",
        "tests/test_test_architecture_contract.py",
    } <= paths


def test_canonical_backtest_retains_manual_dispatch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "  workflow_dispatch:" in text
    assert "runs-on: [self-hosted, backtester]" in text
