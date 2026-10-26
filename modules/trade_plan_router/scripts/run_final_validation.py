from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from zipfile import ZipFile


MODULE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = MODULE_DIR.parents[1]
REPORT_DIR = REPO_DIR / "reports" / "trade_plan_router" / "final_gate"
RUNTIME_DIR = REPORT_DIR / "runtime"
PYTHON = Path(sys.executable)
SOURCE_DATE_EPOCH = "315532800"


class GateError(RuntimeError):
    pass


def run(
    name: str,
    command: list[str],
    *,
    cwd: Path = MODULE_DIR,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout
    (REPORT_DIR / f"{name}.txt").write_text(output, encoding="utf-8")
    if completed.returncode != 0:
        raise GateError(f"{name} failed with exit code {completed.returncode}\n{output[-4000:]}")
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_command(*args: str) -> list[str]:
    return [str(PYTHON), *args]


def deterministic_replay() -> dict[str, Any]:
    outputs: dict[str, str] = {}
    for timezone, seed in (
        ("UTC", "1"),
        ("Europe/Amsterdam", "9173"),
        ("Asia/Bangkok", "99991"),
        ("America/New_York", "31337"),
    ):
        environment = dict(os.environ)
        environment.update({"TZ": timezone, "PYTHONHASHSEED": seed})
        completed = subprocess.run(
            python_command("scripts/deterministic_replay.py"),
            cwd=MODULE_DIR,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise GateError(f"deterministic replay failed for {timezone}: {completed.stdout}")
        outputs[seed] = completed.stdout
        (REPORT_DIR / f"replay-{seed}.json").write_text(completed.stdout, encoding="utf-8")
    reference = outputs["1"]
    if any(value != reference for value in outputs.values()):
        raise GateError("deterministic replay differs across TZ/hash-seed profiles")
    return {
        "status": "PASS",
        "profiles": len(outputs),
        "sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
    }


def benchmark_and_fuzz() -> dict[str, Any]:
    environment = dict(os.environ)
    benchmark_dir = MODULE_DIR / "benchmarks"
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(MODULE_DIR / "src"),
                str(benchmark_dir),
                environment.get("PYTHONPATH", ""),
            ),
        )
    )
    serialization_path = REPORT_DIR / "serialization-fuzz.json"
    risk_path = REPORT_DIR / "risk-reduction-fuzz.json"
    run(
        "serialization-fuzz-console",
        python_command(
            "benchmarks/fuzz_serialization.py",
            "--seed",
            "28023",
            "--cases",
            "5000",
            "--output",
            str(serialization_path),
        ),
        env=environment,
    )
    run(
        "risk-reduction-fuzz-console",
        python_command(
            "benchmarks/fuzz_risk_reduction.py",
            "--seed",
            "24023",
            "--cases",
            "5000",
            "--output",
            str(risk_path),
        ),
        env=environment,
    )
    inline = r"""
import json
from pathlib import Path
from benchmark_router import DecisionRouter, RouterConfiguration, benchmark_case, package
from stress_router import _duplicate_delivery, _soak, _unique_delivery
router = DecisionRouter(RouterConfiguration(max_entry_candidates=1000, max_decisions_per_type=2000))
performance = {
    'schema_version': '1',
    'cases': {
        'candidates_1': benchmark_case(router, package(1), 50),
        'candidates_10': benchmark_case(router, package(10), 30),
        'candidates_100': benchmark_case(router, package(100), 10),
    },
}
stress = {
    'schema_version': '1',
    'duplicate_delivery': _duplicate_delivery(workers=8, deliveries=500),
    'unique_delivery': _unique_delivery(workers=8, packages=250),
    'soak': _soak(iterations=2000),
    'status': 'PASS',
}
Path(r'__PERFORMANCE__').write_text(json.dumps(performance, indent=2, sort_keys=True) + '\n', encoding='utf-8')
Path(r'__STRESS__').write_text(json.dumps(stress, indent=2, sort_keys=True) + '\n', encoding='utf-8')
print('FAST_PERFORMANCE_AND_STRESS_PASS')
""".replace("__PERFORMANCE__", str(REPORT_DIR / "performance.json")).replace(
        "__STRESS__", str(REPORT_DIR / "stress.json")
    )
    run("performance-stress", python_command("-c", inline), env=environment)
    return {
        "serialization_fuzz": json.loads(serialization_path.read_text(encoding="utf-8")),
        "risk_reduction_fuzz": json.loads(risk_path.read_text(encoding="utf-8")),
        "performance": json.loads((REPORT_DIR / "performance.json").read_text(encoding="utf-8")),
        "stress": json.loads((REPORT_DIR / "stress.json").read_text(encoding="utf-8")),
    }


def build_wheels() -> dict[str, Any]:
    wheel_root = RUNTIME_DIR / "wheels"
    first = wheel_root / "first"
    second = wheel_root / "second"
    wheelhouse = wheel_root / "wheelhouse"
    for path in (first, second, wheelhouse):
        path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "PYTHONHASHSEED": "0",
        }
    )
    for label, output in (("first", first), ("second", second)):
        run(
            f"wheel-core-{label}",
            python_command(
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                str(REPO_DIR),
                "-w",
                str(output),
            ),
            cwd=REPO_DIR,
            env=environment,
        )
        run(
            f"wheel-router-{label}",
            python_command(
                "-m",
                "pip",
                "wheel",
                "--no-build-isolation",
                "--no-deps",
                str(MODULE_DIR),
                "-w",
                str(output),
            ),
            env=environment,
        )

    def one(path: Path, pattern: str) -> Path:
        values = tuple(path.glob(pattern))
        if len(values) != 1:
            raise GateError(f"expected one {pattern} in {path}, found {len(values)}")
        return values[0]

    first_core = one(first, "modular_strategy_core-*.whl")
    second_core = one(second, "modular_strategy_core-*.whl")
    first_router = one(first, "modular_strategy_trade_plan_router-*.whl")
    second_router = one(second, "modular_strategy_trade_plan_router-*.whl")
    hashes = {
        "core_first": sha256(first_core),
        "core_second": sha256(second_core),
        "router_first": sha256(first_router),
        "router_second": sha256(second_router),
    }
    if hashes["core_first"] != hashes["core_second"]:
        raise GateError("Core wheels are not byte-identical")
    if hashes["router_first"] != hashes["router_second"]:
        raise GateError("Router wheels are not byte-identical")

    with ZipFile(first_router) as archive:
        names = tuple(archive.namelist())
    if "modular_strategy_trade_plan_router/py.typed" not in names:
        raise GateError("Router wheel does not contain py.typed")
    for forbidden in (
        "tests/",
        "reports/",
        ".coverage",
        ".pytest_cache",
        "__pycache__",
    ):
        if any(forbidden in name for name in names):
            raise GateError(f"Router wheel contains forbidden path: {forbidden}")
    if any(name.startswith("/") or ".." in Path(name).parts for name in names):
        raise GateError("Router wheel contains unsafe path")

    run(
        "wheel-pyyaml",
        python_command(
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "PyYAML>=6.0",
            "-w",
            str(wheelhouse),
        ),
        cwd=REPO_DIR,
    )
    core_wheel = wheelhouse / first_core.name
    router_wheel = wheelhouse / first_router.name
    shutil.copy2(first_core, core_wheel)
    shutil.copy2(first_router, router_wheel)
    return {
        "status": "PASS",
        "hashes": hashes,
        "router_wheel": str(router_wheel),
        "core_wheel": str(core_wheel),
        "wheelhouse": str(wheelhouse),
        "router_entries": len(names),
    }


def clean_install(wheels: dict[str, Any]) -> dict[str, Any]:
    venv_dir = RUNTIME_DIR / "clean-venv"
    run(
        "clean-venv-create",
        python_command("-m", "venv", str(venv_dir)),
        cwd=REPO_DIR,
    )
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheelhouse = Path(wheels["wheelhouse"])
    core_wheel = Path(wheels["core_wheel"])
    router_wheel = Path(wheels["router_wheel"])
    pyyaml_wheels = tuple(wheelhouse.glob("PyYAML-*.whl")) + tuple(wheelhouse.glob("pyyaml-*.whl"))
    if len(pyyaml_wheels) != 1:
        raise GateError(f"expected one PyYAML wheel in {wheelhouse}, found {len(pyyaml_wheels)}")
    run(
        "clean-install",
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(pyyaml_wheels[0]),
            str(core_wheel),
            str(router_wheel),
        ],
        cwd=RUNTIME_DIR,
    )
    run(
        "clean-pip-check",
        [str(venv_python), "-m", "pip", "check"],
        cwd=RUNTIME_DIR,
    )
    import_probe = """
from importlib.metadata import entry_points, version
from modular_strategy_trade_plan_router import TradePlanRouterModule, TradePlanSerializer
module = TradePlanRouterModule()
assert module.manifest.name == 'trade_plan_router'
assert module.snapshot_state() == {}
assert TradePlanSerializer().max_payload_bytes > 0
points = [p for p in entry_points(group='modular_strategy.modules') if p.name == 'trade_plan_router']
assert len(points) == 1
assert points[0].load() is TradePlanRouterModule
print(version('modular-strategy-trade-plan-router'))
"""
    run("clean-import", [str(venv_python), "-c", import_probe], cwd=RUNTIME_DIR)
    run(
        "clean-uninstall",
        [
            str(venv_python),
            "-m",
            "pip",
            "uninstall",
            "-y",
            "modular-strategy-trade-plan-router",
        ],
        cwd=RUNTIME_DIR,
    )
    uninstall_probe = """
from importlib.metadata import entry_points
from importlib.util import find_spec
assert find_spec('modular_strategy_trade_plan_router') is None
assert not [p for p in entry_points(group='modular_strategy.modules') if p.name == 'trade_plan_router']
print('UNINSTALL_PASS')
"""
    run(
        "clean-uninstall-probe",
        [str(venv_python), "-c", uninstall_probe],
        cwd=RUNTIME_DIR,
    )
    run(
        "clean-reinstall",
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(router_wheel),
        ],
        cwd=RUNTIME_DIR,
    )
    run(
        "clean-pip-check-reinstall",
        [str(venv_python), "-m", "pip", "check"],
        cwd=RUNTIME_DIR,
    )
    run("clean-reimport", [str(venv_python), "-c", import_probe], cwd=RUNTIME_DIR)
    return {"status": "PASS", "python": str(venv_python)}


def audits() -> dict[str, Any]:
    run(
        "security-scan",
        python_command("scripts/security_scan.py", "--production-only"),
    )
    run("type-surface", python_command("scripts/type_surface_audit.py"))
    run("golden-matrix", python_command("scripts/verify_golden_matrix.py"))
    run(
        "bandit",
        python_command(
            "-m",
            "bandit",
            "-r",
            "src/modular_strategy_trade_plan_router",
            "-q",
            "-f",
            "json",
        ),
    )
    run(
        "pip-audit",
        python_command("-m", "pip_audit", "--local", "--skip-editable"),
        cwd=REPO_DIR,
    )
    return {"status": "PASS"}


def main() -> int:
    if REPORT_DIR.exists():
        shutil.rmtree(REPORT_DIR)
    REPORT_DIR.mkdir(parents=True)
    RUNTIME_DIR.mkdir(parents=True)
    report: dict[str, Any] = {
        "schema_version": "1",
        "python": sys.version,
        "platform": sys.platform,
        "status": "RUNNING",
    }
    try:
        report["audits"] = audits()
        report["deterministic_replay"] = deterministic_replay()
        report["benchmarks"] = benchmark_and_fuzz()
        wheels = build_wheels()
        report["wheels"] = wheels
        report["clean_install"] = clean_install(wheels)
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        (REPORT_DIR / "FINAL_GATE.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise
    (REPORT_DIR / "FINAL_GATE.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
