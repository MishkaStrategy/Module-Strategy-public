from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / ".github" / "workflows" / "canonical-strategy-v2-backtest.yml"
FUNDING = ROOT / ".github" / "workflows" / "ms5a-funding-owner-v5-materialization.yml"
HISTORICAL_OWNER = (
    ROOT / ".github" / "workflows" / "historical-data-persistent-registration-smoke-v2.yml"
)
POLICY = ROOT / ".github" / "OWNER_DATA_CI_POLICY.md"
POLICY_TEST = "tests/ci_policy/test_owner_data_workflow_guards.py"
ROUTING_AUDIT = ROOT / "scripts" / "audit_runner_routing.py"
WORKFLOWS = {
    CANONICAL: ("owner-authority-unavailable", "official-binance-backtest"),
    FUNDING: ("owner-authority-unavailable", "materialize"),
    HISTORICAL_OWNER: ("owner-authority-unavailable", "register-and-smoke"),
}


def _workflow(path: Path) -> dict[str, object]:
    # BaseLoader preserves the GitHub Actions `on` key as a string.
    parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _jobs(workflow: dict[str, object]) -> dict[str, dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert all(isinstance(job, dict) for job in jobs.values())
    return jobs


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _event_matrix(
    diagnostic_if: str,
    native_job: dict[str, object],
    automatic_if: str,
    automatic_name: str,
    diagnostic_name: str,
    events: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """Model only the three reviewed event names and GitHub's default needs success."""
    matrix: dict[str, tuple[str, ...]] = {}
    for event in events:
        diagnostic_runs = diagnostic_if == f"github.event_name == '{event}'"
        native_if = native_job.get("if") == f"github.event_name == '{event}'"
        needs = native_job.get("needs") == [diagnostic_name]
        # Dispatch diagnostic is designed to fail unconditionally, so a dependent
        # job cannot run under GitHub's implicit success() dependency condition.
        diagnostic_succeeded = False
        native_runs = diagnostic_succeeded and native_if and needs
        automatic_runs = f"github.event_name == '{event}'" in automatic_if.split(" || ")
        matrix[event] = tuple(
            name
            for name, selected in (
                (automatic_name, automatic_runs),
                (diagnostic_name, diagnostic_runs),
                ("native", native_runs),
            )
            if selected
        )
    return matrix


def _job(text: str, name: str, following: str) -> str:
    start = text.index(f"  {name}:\n")
    end = text.index(f"  {following}:\n", start)
    return text[start:end]


def _trigger_paths(text: str, event: str) -> str:
    lines = text.splitlines()
    start = lines.index(f"  {event}:")
    end = start + 1
    while end < len(lines) and (not lines[end].startswith("  ") or lines[end].startswith("    ")):
        end += 1
    return "\n".join(lines[start:end])


def test_canonical_automatic_path_is_synthetic_and_keeps_its_guard_self_trigger() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    paths = _trigger_paths(text, "pull_request")
    job = _job(text, "synthetic-compatibility", "owner-authority-unavailable")

    assert '".github/workflows/canonical-strategy-v2-backtest.yml"' in paths
    assert f'"{POLICY.relative_to(ROOT).as_posix()}"' in paths
    assert f'"{POLICY_TEST}"' in paths
    assert "if: github.event_name == 'pull_request'" in job
    assert "role: fast" in job
    assert "PYTHONPATH: research/strategy_audit_validation" in job
    assert "research/strategy_audit_validation/benchmarks/tests" in job
    assert "test_owner_data_workflow_guards.py" in job
    for forbidden in (
        "official_usdtm_dataset",
        "registered_canonical_dataset",
        "ModuleStrategyData",
        "python -m benchmarks.canonical_strategy_v2",
        "publish-release-evidence",
        "$HOME",
    ):
        assert forbidden not in job


def test_canonical_owner_execution_is_disabled_and_dispatch_fails_before_work() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    owner_job = text[text.index("  official-binance-backtest:\n") :]
    diagnostic = _job(text, "owner-authority-unavailable", "official-binance-backtest")
    owner_header = owner_job.split("    steps:\n", 1)[0]

    assert "workflow_dispatch:" in text
    assert "permissions:\n  contents: read" in text
    assert "needs: [owner-authority-unavailable]" in owner_header
    assert "if: github.event_name == 'workflow_dispatch'" in owner_header
    assert "always()" not in owner_header
    assert "failure()" not in owner_header
    assert "continue-on-error" not in text
    assert "permissions:\n      contents: write" in owner_job
    assert '--storage-root "$HOME/ModuleStrategyData/market_data"' in owner_job
    assert "Run deterministic full-corpus backtest twice" in owner_job
    assert "if: github.event_name == 'workflow_dispatch'" in diagnostic
    assert "runs-on: [self-hosted, fast]" in diagnostic
    assert "Manual dispatch is not authorization" in diagnostic
    assert "exit 1" in diagnostic
    assert "official_usdtm_dataset" not in diagnostic
    assert "actions/checkout" not in diagnostic
    assert "ModuleStrategyData" not in diagnostic
    assert "publish-release-evidence" not in diagnostic


def test_funding_automatic_push_is_read_only_and_owner_job_is_disabled() -> None:
    text = FUNDING.read_text(encoding="utf-8")
    push_paths = _trigger_paths(text, "push")
    contract = _job(text, "contract", "materialize")
    owner_job = _job(text, "materialize", "owner-authority-unavailable")
    owner_header = owner_job.split("    steps:\n", 1)[0]
    diagnostic = text[text.index("  owner-authority-unavailable:\n") :]

    for path in (
        '".github/workflows/ms5a-funding-owner-v5-materialization.yml"',
        f'"{POLICY.relative_to(ROOT).as_posix()}"',
        f'"{POLICY_TEST}"',
    ):
        assert path in push_paths
    assert "github.event_name == 'pull_request' || github.event_name == 'push'" in contract
    assert "--mode check" in contract
    assert "--mode materialize" not in contract
    assert "--storage-root" not in contract
    assert "test_funding_v5_materialization.py" in contract
    assert "needs: [owner-authority-unavailable]" in owner_header
    assert "if: github.event_name == 'workflow_dispatch'" in owner_header
    assert "always()" not in owner_header
    assert "failure()" not in owner_header
    assert "runs-on: [self-hosted, fast]" in owner_job
    assert "--mode materialize" in owner_job
    assert '--storage-root "$HOME/ModuleStrategyData/market_data"' in owner_job
    assert "[self-hosted, macOS, ARM64]" not in text
    assert "workflow_dispatch:" in text
    assert "if: github.event_name == 'workflow_dispatch'" in diagnostic
    assert "runs-on: [self-hosted, fast]" in diagnostic
    assert "Manual dispatch is not authorization" in diagnostic
    assert "exit 1" in diagnostic
    assert "actions/checkout" not in diagnostic
    assert "--mode materialize" not in diagnostic
    assert "ModuleStrategyData" not in diagnostic


def test_historical_owner_workflow_uses_synthetic_dag_for_pr_and_push() -> None:
    text = HISTORICAL_OWNER.read_text(encoding="utf-8")
    push = _trigger_paths(text, "push")
    pull_request = _trigger_paths(text, "pull_request")
    synthetic = _job(text, "synthetic-contract", "owner-authority-unavailable")
    owner_job = text[text.index("  register-and-smoke:\n") :]
    owner_header = owner_job.split("    steps:\n", 1)[0]
    diagnostic = _job(text, "owner-authority-unavailable", "register-and-smoke")

    assert (
        '      - ".github/workflows/historical-data-persistent-registration-smoke-v2.yml"' in push
    )
    assert f'      - "{POLICY.relative_to(ROOT).as_posix()}"' in push
    assert f'      - "{POLICY_TEST}"' in push
    assert (
        '      - ".github/workflows/historical-data-persistent-registration-smoke-v2.yml"'
        in pull_request
    )
    assert f'      - "{POLICY.relative_to(ROOT).as_posix()}"' in pull_request
    assert f'      - "{POLICY_TEST}"' in pull_request
    for path in (
        "tests/backtest_engine/test_registered_config.py",
        "tests/backtest_engine/test_registered_data.py",
        "modules/market_data_ingestion/tests/test_dataset_reader.py",
    ):
        assert path in push
        assert path in pull_request
    assert "github.event_name == 'pull_request' || github.event_name == 'push'" in synthetic
    assert "runs-on: [self-hosted, fast]" in synthetic
    for path in (
        "tests/backtest_engine/test_acceptance_noop.py",
        "tests/backtest_engine/test_registered_config.py",
        "tests/backtest_engine/test_registered_data.py",
        "modules/market_data_ingestion/tests/test_dataset_reader.py",
        "tests/ci_policy/test_owner_data_workflow_guards.py",
    ):
        assert path in synthetic
    assert "python scripts/run_persistent_registration_smoke_v2.py" not in synthetic
    assert "needs: [owner-authority-unavailable]" in owner_header
    assert "if: github.event_name == 'workflow_dispatch'" in owner_header
    assert "always()" not in owner_header
    assert "failure()" not in owner_header
    assert "runs-on: [self-hosted, backtester]" in owner_header
    assert "permissions:\n      contents: write" in owner_header
    assert "role: backtester" in owner_job
    assert "role: macos" not in owner_job
    assert "python scripts/run_persistent_registration_smoke_v2.py" in owner_job
    assert 'Path(".github/workflows/historical-data-persistent-registration-smoke-v2.yml")' in (
        ROUTING_AUDIT.read_text(encoding="utf-8")
    )
    assert "if: github.event_name == 'workflow_dispatch'" in diagnostic
    assert "runs-on: [self-hosted, fast]" in diagnostic
    assert "exit 1" in diagnostic
    assert "actions/checkout" not in diagnostic
    assert "ModuleStrategyData" not in diagnostic
    assert "permissions:\n  contents: read" in text


def test_policy_states_temporary_fail_closed_scope_and_synthetic_boundary() -> None:
    text = POLICY.read_text(encoding="utf-8")

    assert "patched integration revision" in text
    assert "does not update `main` or other refs" in text
    assert "No standing authority" in text
    assert "manual-only authority" in text
    assert "native job requires" in text
    assert "dispatch button, input, branch name" in text
    assert "is not authorization" in text
    assert "operation-specific protocol" in text
    assert "MS5A search" in text
    assert "use only synthetic" in text


@pytest.mark.parametrize(
    ("path", "diagnostic_name", "native_name", "automatic_name", "native_publishes"),
    (
        (
            CANONICAL,
            "owner-authority-unavailable",
            "official-binance-backtest",
            "synthetic-compatibility",
            True,
        ),
        (FUNDING, "owner-authority-unavailable", "materialize", "contract", False),
        (
            HISTORICAL_OWNER,
            "owner-authority-unavailable",
            "register-and-smoke",
            "synthetic-contract",
            True,
        ),
    ),
)
def test_parsed_owner_workflow_dag_fails_closed_for_pr_push_and_dispatch(
    path: Path,
    diagnostic_name: str,
    native_name: str,
    automatic_name: str,
    native_publishes: bool,
) -> None:
    workflow = _workflow(path)
    jobs = _jobs(workflow)
    diagnostic = jobs[diagnostic_name]
    native = jobs[native_name]
    automatic = jobs[automatic_name]

    assert workflow["permissions"] == {"contents": "read"}
    expected_native_permissions = {"contents": "write"} if native_publishes else None
    assert native.get("permissions") == expected_native_permissions
    assert all(
        job.get("permissions", {}).get("contents") != "write"
        for name, job in jobs.items()
        if name != native_name
    )
    assert diagnostic.get("if") == "github.event_name == 'workflow_dispatch'"
    assert diagnostic.get("runs-on") == ["self-hosted", "fast"]
    diagnostic_steps = _steps(diagnostic)
    assert len(diagnostic_steps) == 1
    step = diagnostic_steps[0]
    assert "run" in step and "uses" not in step
    assert "if" not in step and "continue-on-error" not in step
    assert "continue-on-error" not in diagnostic
    commands = [line.strip() for line in str(step["run"]).splitlines() if line.strip()]
    assert commands[-1] == "exit 1"

    assert native.get("needs") == [diagnostic_name]
    assert native.get("if") == "github.event_name == 'workflow_dispatch'"
    assert "continue-on-error" not in native
    assert not any(
        token in str(native.get("if", ""))
        for token in ("always(", "failure(", "cancelled(", "success(")
    )

    # The automatic job itself is restricted to PR/push and its steps contain
    # no owner-data invocation or publisher. The only other jobs are the
    # unconditional-failure diagnostic and guarded native job above.
    automatic_if = str(automatic.get("if", ""))
    assert "github.event_name == 'pull_request'" in automatic_if or (
        "github.event_name == 'push'" in automatic_if
    )
    assert set(jobs) == {diagnostic_name, native_name, automatic_name}
    for safe_step in _steps(automatic):
        body = str(safe_step.get("run", ""))
        action = str(safe_step.get("uses", ""))
        for forbidden in (
            "official_usdtm_dataset",
            "registered_canonical_dataset",
            "ModuleStrategyData",
            "python scripts/run_persistent_registration_smoke_v2.py",
            "--mode materialize",
            "publish-release-evidence",
            "actions/upload-artifact",
        ):
            assert forbidden not in body
            assert forbidden not in action

    modeled = _event_matrix(
        str(diagnostic["if"]),
        native,
        automatic_if,
        automatic_name,
        diagnostic_name,
        ("pull_request", "push", "workflow_dispatch"),
    )
    assert modeled["pull_request"] == (automatic_name,)
    expected_push = (automatic_name,) if "github.event_name == 'push'" in automatic_if else ()
    assert modeled["push"] == expected_push
    assert modeled["workflow_dispatch"] == (diagnostic_name,)


def test_dispatch_diagnostic_has_no_step_or_job_escape_hatch() -> None:
    for path, (diagnostic_name, native_name) in WORKFLOWS.items():
        jobs = _jobs(_workflow(path))
        for job_name in (diagnostic_name, native_name):
            job = jobs[job_name]
            assert "continue-on-error" not in job
        for step in _steps(jobs[diagnostic_name]):
            assert "continue-on-error" not in step
        diagnostic_steps = _steps(jobs[diagnostic_name])
        assert len(diagnostic_steps) == 1
        assert diagnostic_steps[0].get("if") is None
        assert diagnostic_steps[0].get("uses") is None
