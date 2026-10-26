from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_policy() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "audit_runner_routing.py"
    spec = importlib.util.spec_from_file_location("audit_runner_routing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, name: str, selector: str, extra: str = "") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(
        "name: policy-test\n"
        "jobs:\n"
        "  test:\n"
        f"    runs-on: {selector}\n"
        "    timeout-minutes: 10\n"
        f"{extra}",
        encoding="utf-8",
    )


def _workflow_text(name: str) -> str:
    return (PROJECT_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    assert start in text
    assert end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def test_accepts_canonical_capability_selectors(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(root, "fast.yml", "[self-hosted, fast]")
    _write(root, "docker.yml", "[self-hosted, docker]")
    _write(root, "macos.yml", "[self-hosted, macos]")
    _write(root, "macos-arm64.yml", "[self-hosted, macOS, ARM64]")
    _write(root, "backtest-engine-heavy.yml", "[self-hosted, backtester]")
    _write(root, "heavy-backtest.yml", "[self-hosted, backtester]")
    _write(
        root,
        "historical-data-persistent-registration-smoke.yml",
        "[self-hosted, backtester]",
    )
    _write(
        root,
        "market-data-external-corpus-drive-acceptance.yml",
        "[self-hosted, backtester]",
    )
    _write(
        root,
        "market-data-external-corpus-registration-ci.yml",
        "[self-hosted, backtester]",
    )
    _write(root, "signal-bot-heavy-audit.yml", "[self-hosted, backtester]")
    _write(root, "strategy-factory-soak.yml", "[self-hosted, backtester]")

    assert policy.audit(root) == []


def test_accepts_approved_github_hosted_selectors(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(root, "hosted-linux.yml", "ubuntu-latest")
    _write(root, "hosted-macos.yml", "macos-latest")

    assert policy.audit(root) == []


def test_accepts_reusable_workflow_caller_without_runs_on(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    root.mkdir(parents=True)
    (root / "caller.yml").write_text(
        "name: reusable-caller\njobs:\n  delegated:\n    uses: ./.github/workflows/reusable.yml\n",
        encoding="utf-8",
    )

    assert policy.audit(root) == []


def test_rejects_bare_unsupported_hosted_legacy_and_service_selectors(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(root, "bare.yml", "[self-hosted]")
    _write(root, "unsupported-hosted.yml", "windows-latest")
    _write(root, "legacy.yml", "[self-hosted, backtest]")
    _write(root, "service.yml", "[self-hosted, postgresql]")

    errors = policy.audit(root)
    rendered = "\n".join(errors)
    assert "bare self-hosted" in rendered
    assert "windows-latest" in rendered
    assert "backtest" in rendered
    assert "postgresql" in rendered


def test_rejects_unallowlisted_backtester_and_serial_matrix(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(
        root,
        "ordinary.yml",
        "[self-hosted, backtester]",
        "    strategy:\n      max-parallel: 1\n",
    )

    errors = policy.audit(root)
    rendered = "\n".join(errors)
    assert "backtester role is not allowlisted" in rendered
    assert "max-parallel: 1" in rendered


def test_requires_at_least_one_job_selector(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    root.mkdir(parents=True)
    (root / "empty.yml").write_text("name: empty\n", encoding="utf-8")

    assert policy.audit(root) == [".github/workflows/empty.yml: no runs-on selector found"]


def test_rejects_empty_workflow_directory(tmp_path: Path) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    root.mkdir(parents=True)

    assert policy.audit(root) == [f"no workflow files found under {root}"]


def test_requires_quantitative_allowlist_entries_to_use_backtester(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(root, "heavy-backtest.yml", "ubuntu-latest")
    _write(root, "signal-bot-heavy-audit.yml", "macos-latest")
    _write(root, "strategy-factory-soak.yml", "[self-hosted, fast]")

    errors = policy.audit(root)
    assert len(errors) == 3
    assert all("allowlisted quantitative workflow" in error for error in errors)
    assert all("[self-hosted, backtester]" in error for error in errors)


def test_workflow_discovery_supports_yaml_and_ignores_directories(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    root = tmp_path / ".github" / "workflows"
    _write(root, "z-last.yaml", "[self-hosted, fast]")
    _write(root, "a-first.yml", "ubuntu-latest")
    (root / "ignored.yml").mkdir()

    assert [path.name for path in policy.workflow_files(root)] == [
        "a-first.yml",
        "z-last.yaml",
    ]


def test_main_reports_success(monkeypatch, capsys) -> None:
    policy = _load_policy()
    monkeypatch.setattr(policy, "audit", list)

    assert policy.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "Runner routing policy audit passed.\n"
    assert captured.err == ""


def test_main_reports_each_violation(monkeypatch, capsys) -> None:
    policy = _load_policy()
    monkeypatch.setattr(policy, "audit", lambda: ["first", "second"])

    assert policy.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("Runner routing policy violations:\n- first\n- second\n")


def test_decision_router_pr_triggers_are_path_scoped() -> None:
    names = (
        "decision-router-final-gate.yml",
        "decision-router-core-v042-compatibility.yml",
    )
    for name in names:
        text = _workflow_text(name)
        pull_request = _section(text, "  pull_request:\n", "\n\npermissions:")
        assert "    paths:\n" in pull_request
        assert '      - "modules/trade_plan_router/**"\n' in pull_request
        assert f'      - ".github/workflows/{name}"' in pull_request
        assert "github.event.pull_request.head.ref == 'modules/trade-plan-router-v0.5'" in text


def test_core_pr_ignores_signal_bot_and_policy_only_tests_but_push_keeps_them() -> None:
    text = _workflow_text("ci.yml")
    push = _section(text, "  push:\n", "  pull_request:\n")
    pull_request = _section(text, "  pull_request:\n", "\n\npermissions:")

    assert '      - "tests/**"\n' in pull_request
    assert '      - "!tests/signal_bot/**"\n' in pull_request
    assert '      - "!tests/ci_policy/**"\n' in pull_request
    assert '      - "tests/**"\n' in push
    assert "!tests/signal_bot/**" not in push
    assert "!tests/ci_policy/**" not in push


def test_strategy_factory_pr_ignores_signal_bot_only_source_but_keeps_broad_scope() -> None:
    text = _workflow_text("strategy-factory-ci.yml")
    pull_request = _section(text, "  pull_request:\n", "  push:\n")
    push = _section(text, "  push:\n", "  workflow_dispatch:\n")

    assert '      - "src/**"\n' in pull_request
    assert '      - "!src/signal_bot/**"\n' in pull_request
    assert "!src/signal_bot/**" not in push
