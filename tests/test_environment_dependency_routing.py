from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_policy() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "audit_docker_runner_routing.py"
    spec = importlib.util.spec_from_file_location("audit_environment_routing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow(root: Path, name: str, selector: str, step: str) -> None:
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "name: semantic-test\n"
        "jobs:\n"
        "  verify:\n"
        f"    runs-on: {selector}\n"
        "    timeout-minutes: 10\n"
        "    steps:\n"
        "      - run: |\n"
        f"          {step}\n",
        encoding="utf-8",
    )


def test_accepts_matching_self_hosted_docker_and_macos_roles(tmp_path: Path) -> None:
    policy = _load_policy()
    _workflow(tmp_path, "docker.yml", "[self-hosted, docker]", "docker info")
    _workflow(tmp_path, "macos.yml", "[self-hosted, macos]", "xcodebuild -version")

    assert policy.audit(tmp_path) == []


def test_accepts_matching_github_hosted_docker_and_macos_runners(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    _workflow(tmp_path, "docker.yml", "ubuntu-latest", "docker info")
    _workflow(tmp_path, "macos.yml", "macos-latest", "xcodebuild -version")

    assert policy.audit(tmp_path) == []


def test_rejects_docker_dependency_on_fast_runner(tmp_path: Path) -> None:
    policy = _load_policy()
    _workflow(tmp_path, "wrong-docker.yml", "[self-hosted, fast]", "docker compose up -d")

    assert policy.audit(tmp_path) == [
        (
            ".github/workflows/wrong-docker.yml:verify: Docker/container dependency "
            "requires one of [self-hosted, docker], ubuntu-latest, "
            "found '[self-hosted, fast]'"
        )
    ]


def test_rejects_macos_dependency_on_fast_runner(tmp_path: Path) -> None:
    policy = _load_policy()
    _workflow(tmp_path, "wrong-macos.yml", "[self-hosted, fast]", "xcrun --show-sdk-path")

    assert policy.audit(tmp_path) == [
        (
            ".github/workflows/wrong-macos.yml:verify: macOS-specific dependency "
            "requires one of [self-hosted, macos], macos-latest, "
            "found '[self-hosted, fast]'"
        )
    ]


def test_rejects_incompatible_docker_and_macos_topology(tmp_path: Path) -> None:
    policy = _load_policy()
    _workflow(
        tmp_path,
        "mixed.yml",
        "[self-hosted, docker]",
        "docker info && xcodebuild -version",
    )

    errors = policy.audit(tmp_path)
    assert any("macOS-specific dependency" in error for error in errors)
    assert any("split the incompatible topology" in error for error in errors)


def test_does_not_treat_postgres_url_as_runner_label_requirement(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    _workflow(
        tmp_path,
        "database-client.yml",
        "[self-hosted, fast]",
        "python -c \"print('postgresql://localhost/test')\"",
    )

    assert policy.audit(tmp_path) == []


def test_jobs_parser_handles_missing_jobs_section() -> None:
    policy = _load_policy()

    assert policy._jobs("name: no-jobs\non: push\n") == []


def test_jobs_parser_collects_multiple_jobs_and_stops_at_next_top_level() -> None:
    policy = _load_policy()
    text = (
        "name: parser-test\n"
        "jobs:\n"
        "  first:\n"
        "    runs-on: [self-hosted, fast]\n"
        "  second: # inline comment\n"
        "    runs-on: ubuntu-latest\n"
        "permissions:\n"
        "  contents: read\n"
    )

    jobs = policy._jobs(text)
    assert [name for name, _ in jobs] == ["first", "second"]
    assert policy._selector(jobs[0][1]) == "[self-hosted, fast]"
    assert policy._selector(jobs[1][1]) == "ubuntu-latest"


def test_selector_returns_none_when_job_has_no_runner() -> None:
    policy = _load_policy()

    assert policy._selector("  verify:\n    steps:\n      - run: true") is None


def test_audit_ignores_workflow_without_jobs_and_job_without_selector(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "no-jobs.yml").write_text("name: no-jobs\non: push\n", encoding="utf-8")
    (directory / "no-selector.yaml").write_text(
        "name: no-selector\njobs:\n  verify:\n    steps:\n      - run: true\n",
        encoding="utf-8",
    )
    (directory / "ignored.yml").mkdir()

    assert policy.audit(tmp_path) == []


def test_detects_container_service_and_docker_action_dependencies(
    tmp_path: Path,
) -> None:
    policy = _load_policy()
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "semantic.yml").write_text(
        "name: semantic\n"
        "jobs:\n"
        "  container_job:\n"
        "    runs-on: [self-hosted, fast]\n"
        "    container: python:3.13-slim\n"
        "  service_job:\n"
        "    runs-on: [self-hosted, fast]\n"
        "    services:\n"
        "      redis:\n"
        "        image: redis:7\n"
        "  action_job:\n"
        "    runs-on: [self-hosted, fast]\n"
        "    steps:\n"
        "      - uses: docker/setup-buildx-action@v3\n",
        encoding="utf-8",
    )

    errors = policy.audit(tmp_path)
    assert len(errors) == 3
    assert all("Docker/container dependency" in error for error in errors)


def test_main_reports_success(monkeypatch, capsys) -> None:
    policy = _load_policy()
    monkeypatch.setattr(policy.sys, "argv", ["audit-environment-routing"])
    monkeypatch.setattr(policy, "audit", lambda repo: [])

    assert policy.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "Environment dependency routing policy passed.\n"
    assert captured.err == ""


def test_main_reports_each_violation(monkeypatch, capsys, tmp_path: Path) -> None:
    policy = _load_policy()
    monkeypatch.setattr(
        policy.sys,
        "argv",
        ["audit-environment-routing", str(tmp_path)],
    )
    monkeypatch.setattr(policy, "audit", lambda repo: [f"{repo}: first", "second"])

    assert policy.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"Environment dependency routing violations:\n- {tmp_path.resolve()}: first\n- second\n"
    )
