from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv
from importlib.metadata import version
from typing import Mapping, Sequence


REPO_DIR = Path(__file__).resolve().parents[3]
MODULE_DIR = REPO_DIR / "modules" / "trade_plan_router"
CORE_DIR = REPO_DIR / ".contract-deps" / "core"
REPORT_DIR = REPO_DIR / "reports" / "trade_plan_router" / "core_v042_compatibility"
RUNTIME_DIR = REPORT_DIR / "runtime"
WHEELHOUSE = RUNTIME_DIR / "wheelhouse"
CLEAN_VENV = RUNTIME_DIR / "clean-venv"
CORE_SHA = "04f6de01e5337883256299dd20effdbedd5ac73e"


def _render(completed: subprocess.CompletedProcess[str]) -> str:
    output = completed.stdout
    if completed.stderr:
        if output and not output.endswith("\n"):
            output += "\n"
        output += completed.stderr
    return output


def run(
    label: str,
    command: Sequence[str],
    *,
    cwd: Path = REPO_DIR,
    env: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=None if env is None else dict(env),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rendered = _render(completed)
    REPORT_DIR.joinpath(f"{label}.txt").write_text(rendered, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")
    return rendered


def _python_in(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _write_inputs() -> None:
    router_head = os.environ.get("ROUTER_HEAD_SHA", "")
    if len(router_head) != 40:
        raise RuntimeError("ROUTER_HEAD_SHA must be an exact commit SHA")
    if version("modular-strategy-core") != "0.4.2":
        raise RuntimeError("validated Core 0.4.2 is not installed")
    if version("modular-strategy-trade-plan-router") != "0.1.0":
        raise RuntimeError("Router 0.1.0 is not installed")
    REPORT_DIR.joinpath("INPUTS.txt").write_text(
        f"router_head_sha={router_head}\n"
        f"core_head_sha={CORE_SHA}\n"
        "core_version=0.4.2\n"
        "router_version=0.1.0\n",
        encoding="utf-8",
    )


def _run_source_checks() -> None:
    run(
        "compile",
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            str(MODULE_DIR / "src"),
            str(MODULE_DIR / "tests"),
        ],
    )
    run(
        "ruff",
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(MODULE_DIR / "src"),
            str(MODULE_DIR / "tests"),
        ],
    )
    run(
        "mypy",
        [
            sys.executable,
            "-m",
            "mypy",
            str(MODULE_DIR / "src" / "modular_strategy_trade_plan_router"),
        ],
    )
    run("coverage-erase", [sys.executable, "-m", "coverage", "erase"])
    run(
        "pytest",
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            f"--source={MODULE_DIR / 'src' / 'modular_strategy_trade_plan_router'}",
            "-m",
            "pytest",
            "-q",
            str(MODULE_DIR / "tests"),
        ],
    )
    run(
        "coverage",
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--fail-under=90",
            "-m",
        ],
    )


def _build_wheelhouse() -> None:
    WHEELHOUSE.mkdir(parents=True, exist_ok=True)
    for source in (CORE_DIR, MODULE_DIR):
        run(
            f"wheel-{source.name}",
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                str(source),
                "-w",
                str(WHEELHOUSE),
            ],
        )
    run(
        "wheel-pyyaml",
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "PyYAML>=6.0",
            "-w",
            str(WHEELHOUSE),
        ],
    )
    wheels = sorted(WHEELHOUSE.glob("*.whl"))
    if len(wheels) < 3:
        raise RuntimeError("compatibility wheelhouse is incomplete")
    hashes = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in wheels
    )
    REPORT_DIR.joinpath("wheel-hashes.sha256").write_text(
        hashes,
        encoding="utf-8",
    )


def _clean_install() -> None:
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(CLEAN_VENV)
    clean_python = _python_in(CLEAN_VENV)
    run(
        "clean-install",
        [
            str(clean_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(WHEELHOUSE),
            "modular-strategy-core==0.4.2",
            "modular-strategy-trade-plan-router==0.1.0",
            "PyYAML",
        ],
        cwd=RUNTIME_DIR,
    )
    run(
        "pip-check",
        [str(clean_python), "-m", "pip", "check"],
        cwd=RUNTIME_DIR,
    )
    probe = """
from importlib.metadata import version
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamPlanEnvelope,
    TradePlanRouterModule,
)
assert version('modular-strategy-core') == '0.4.2'
assert version('modular-strategy-trade-plan-router') == '0.1.0'
assert TradePlanRouterModule().manifest.name == 'trade_plan_router'
assert DecisionRouter is not None
assert DownstreamPlanEnvelope is not None
print('router_core_v042_clean_install: PASS')
"""
    run(
        "import-smoke",
        [str(clean_python), "-c", probe],
        cwd=RUNTIME_DIR,
    )


def main() -> int:
    if REPORT_DIR.exists():
        shutil.rmtree(REPORT_DIR)
    REPORT_DIR.mkdir(parents=True)
    RUNTIME_DIR.mkdir(parents=True)
    try:
        _write_inputs()
        _run_source_checks()
        _build_wheelhouse()
        _clean_install()
    except Exception as exc:
        REPORT_DIR.joinpath("RESULT.txt").write_text(
            f"router_core_v042_compatibility: FAIL\nerror={type(exc).__name__}: {exc}\n",
            encoding="utf-8",
        )
        raise
    REPORT_DIR.joinpath("RESULT.txt").write_text(
        "router_core_v042_compatibility: PASS\n",
        encoding="utf-8",
    )
    print("router_core_v042_compatibility: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
