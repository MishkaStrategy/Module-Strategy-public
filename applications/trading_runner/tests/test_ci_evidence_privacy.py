from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SANITIZER = ROOT / "scripts" / "sanitize_ci_evidence.py"


def _run_sanitizer(
    evidence: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SANITIZER), str(evidence)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_sanitize_evidence_directory_redacts_private_runner_data(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    private_home = "/" + "Users" + "/evidence-" + "owner"
    runner_root = "GitHub" + "Actions" + "Runners"
    workspace_segment = "_" + "work" + "/" + "Module" + "-" + "Strategy"
    runner_identity = "sensitive-" + "runner-1234"
    workspace = (
        private_home
        + "/"
        + runner_root
        + "/org/"
        + runner_identity
        + "/"
        + workspace_segment
        + "/Module-Strategy"
    )
    local_uri = "file:" + "//" + workspace + "/dist/package.whl"
    residual_topology = (
        runner_root
        + "/org/another-runner/"
        + workspace_segment
        + "/Module-Strategy/dist/package.whl"
    )
    (evidence / "build.txt").write_text(
        f"built {workspace}/dist/package.whl on {runner_identity}\n"
        f"source={local_uri}\nresidual={residual_topology}\n",
        encoding="utf-8",
    )
    (evidence / "build.exit").write_text("0\n", encoding="utf-8")

    result = _run_sanitizer(
        evidence,
        extra_env={
            "HOME": private_home,
            "GITHUB_WORKSPACE": workspace,
            "RUNNER_NAME": runner_identity,
            "USER": "evidence-" + "owner",
        },
    )

    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    payload = (evidence / "build.txt").read_text(encoding="utf-8")
    manifest = json.loads((evidence / "privacy-status.json").read_text(encoding="utf-8"))
    assert status["status"] == "PASS"
    assert manifest == status
    assert status["files_scanned"] == 2
    assert status["credential_findings"] == 0
    assert status["replacements"] > 0
    assert workspace not in payload
    assert private_home not in payload
    assert runner_identity not in payload
    assert local_uri not in payload
    assert runner_root not in payload
    assert workspace_segment not in payload
    assert residual_topology not in payload
    assert (evidence / "build.exit").read_text(encoding="utf-8") == "0\n"


def test_sanitize_evidence_directory_rejects_credentials(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    token = "gh" + "p_" + "A" * 24
    (evidence / "unsafe.txt").write_text(f"token={token}\n", encoding="utf-8")

    result = _run_sanitizer(evidence)

    assert result.returncode == 1
    assert "credential-like evidence" in result.stderr
    assert token not in result.stderr
    assert not (evidence / "privacy-status.json").exists()


def test_sanitize_evidence_directory_rejects_symlinks(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("safe\n", encoding="utf-8")
    (evidence / "linked.txt").symlink_to(target)

    result = _run_sanitizer(evidence)

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert not (evidence / "privacy-status.json").exists()
