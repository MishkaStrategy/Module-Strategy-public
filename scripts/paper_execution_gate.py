from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = REPOSITORY_ROOT / "modules" / "paper_execution"
MODULE_SOURCE = MODULE_ROOT / "src"
CORE_SOURCE = REPOSITORY_ROOT / "src"
FORBIDDEN_NETWORK_IMPORTS = frozenset(
    {
        "aiohttp",
        "asyncio.streams",
        "binance",
        "ccxt",
        "ftplib",
        "http.client",
        "httpx",
        "imaplib",
        "nntplib",
        "paramiko",
        "poplib",
        "requests",
        "smtplib",
        "socket",
        "telnetlib",
        "urllib.request",
        "websockets",
        "xmlrpc.client",
    }
)


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    paths = [str(MODULE_SOURCE), str(CORE_SOURCE)]
    if existing:
        paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run(arguments: Iterable[str], *, cwd: Path = REPOSITORY_ROOT) -> None:
    command = tuple(arguments)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    rendered = " ".join(command)
    raise RuntimeError(
        f"Paper Execution gate command failed ({result.returncode}): "
        f"{rendered}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _network_security_scan() -> None:
    violations: list[str] = []
    package_root = MODULE_SOURCE / "paper_execution"
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        for imported in sorted(_imported_names(tree)):
            if any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for forbidden in FORBIDDEN_NETWORK_IMPORTS
            ):
                violations.append(f"{path.relative_to(REPOSITORY_ROOT)} imports {imported}")
    if violations:
        raise RuntimeError(
            "Paper Execution paper-only network boundary violated:\n- " + "\n- ".join(violations)
        )


def run_functional_gate() -> None:
    """Compile and execute the complete Paper Execution test suite."""

    _run(
        (
            sys.executable,
            "-m",
            "compileall",
            "-q",
            str(MODULE_SOURCE),
            str(MODULE_ROOT / "tests"),
        )
    )
    _run(
        (
            sys.executable,
            "-m",
            "pytest",
            str(MODULE_ROOT / "tests"),
            "-q",
        )
    )


def run_static_gate() -> None:
    """Run paper-only boundary, Ruff and strict MyPy checks."""

    _network_security_scan()
    _run(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            str(MODULE_SOURCE),
            str(MODULE_ROOT / "tests"),
        )
    )
    _run(
        (
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(MODULE_ROOT / "pyproject.toml"),
            str(MODULE_SOURCE / "paper_execution"),
        )
    )


def run_packaging_gate() -> None:
    """Build exactly one isolated Paper Execution wheel."""

    with tempfile.TemporaryDirectory(prefix="paper-execution-wheel-") as directory:
        _run(
            (
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(MODULE_ROOT),
                "--no-deps",
                "--wheel-dir",
                directory,
            )
        )
        wheels = tuple(Path(directory).glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected exactly one Paper Execution wheel, found {len(wheels)}")


def run_gate() -> None:
    """Run the complete exact-head Paper Execution fast gate."""

    run_functional_gate()
    run_static_gate()
    run_packaging_gate()


if __name__ == "__main__":
    run_gate()
