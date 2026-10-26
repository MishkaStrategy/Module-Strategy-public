from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import venv
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules" / "paper_execution"
REPORTS = ROOT / "reports" / "paper_execution"
DIST = ROOT / "dist" / "paper_execution-component"
REBUILD = ROOT / "dist" / "paper_execution-component-rebuild"
CORE_DIST = ROOT / "dist" / "paper_execution-core"


def run(*args: str, cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def digest_files(root: Path) -> dict[str, str]:
    return {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def _scrub_junit_value(
    value: str,
    *,
    private_roots: tuple[str, ...],
    runner_user: str,
    runner_identities: tuple[str, ...],
) -> str:
    value = re.sub(r"file://[^\s\"'<>]+", "redacted-local-uri", value)
    value = re.sub(r"/Users/[^/\s\"'<>]+", "redacted-home", value)
    value = re.sub(
        r"[A-Za-z]:\\Users\\[^\\\s\"'<>]+",
        "redacted-home",
        value,
    )
    for private_root in private_roots:
        value = value.replace(private_root, "redacted-path")
        value = value.replace(private_root.replace("\\", "/"), "redacted-path")
    if len(runner_user) >= 4:
        value = value.replace(runner_user, "redacted-user")
    for runner_identity in runner_identities:
        value = value.replace(runner_identity, "redacted-runner")
    return value


def sanitize_junit_report(path: Path) -> None:
    private_roots = tuple(
        sorted(
            {
                value
                for key in (
                    "HOME",
                    "RUNNER_TEMP",
                    "GITHUB_WORKSPACE",
                    "RUNNER_TOOL_CACHE",
                    "UV_CACHE_DIR",
                )
                if (value := os.environ.get(key))
            },
            key=len,
            reverse=True,
        )
    )
    runner_user = os.environ.get("USER", "")
    runner_identities = tuple(
        sorted(
            {
                value
                for value in (
                    os.environ.get("RUNNER_NAME"),
                    os.environ.get("HOSTNAME"),
                    os.environ.get("COMPUTERNAME"),
                    socket.gethostname(),
                    platform.node(),
                )
                if value and len(value) >= 4
            },
            key=len,
            reverse=True,
        )
    )

    tree = ElementTree.parse(path)
    root = tree.getroot()
    for element in root.iter():
        for attribute, value in list(element.attrib.items()):
            element.set(
                attribute,
                _scrub_junit_value(
                    value,
                    private_roots=private_roots,
                    runner_user=runner_user,
                    runner_identities=runner_identities,
                ),
            )
        if element.text is not None:
            element.text = _scrub_junit_value(
                element.text,
                private_roots=private_roots,
                runner_user=runner_user,
                runner_identities=runner_identities,
            )
        if element.tail is not None:
            element.tail = _scrub_junit_value(
                element.tail,
                private_roots=private_roots,
                runner_user=runner_user,
                runner_identities=runner_identities,
            )
    for suite in root.iter("testsuite"):
        suite.set("hostname", "redacted-runner")
    tree.write(path, encoding="utf-8", xml_declaration=True)

    payload = path.read_text(encoding="utf-8")
    forbidden: dict[str, str] = {
        "POSIX user home": "/Users/",
        "Windows user home": "\\Users\\",
        "local file URI": "file://",
    }
    forbidden.update(
        {f"private root {index}": value for index, value in enumerate(private_roots, start=1)}
    )
    if len(runner_user) >= 4:
        forbidden["runner user"] = runner_user
    forbidden.update(
        {
            f"runner identity {index}": value
            for index, value in enumerate(runner_identities, start=1)
        }
    )
    leaks = [label for label, marker in forbidden.items() if marker and marker in payload]
    if leaks:
        raise RuntimeError(
            "JUnit evidence privacy sanitization failed: " + ", ".join(sorted(leaks))
        )


def clean_wheel_smoke() -> None:
    smoke = ROOT / ".venv-paper-execution-smoke"
    shutil.rmtree(smoke, ignore_errors=True)
    venv.EnvBuilder(with_pip=True, clear=True).create(smoke)
    python = smoke / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(str(python), "-m", "pip", "install", "--upgrade", "pip")
    wheels = sorted(glob.glob(str(CORE_DIST / "*.whl"))) + sorted(glob.glob(str(DIST / "*.whl")))
    if len(wheels) != 2:
        raise RuntimeError(f"expected core and Paper wheels, found {wheels!r}")
    run(str(python), "-m", "pip", "install", *wheels)
    run(str(python), "-m", "pip", "check")
    run(
        str(python),
        "-c",
        "import paper_execution; print(paper_execution.PaperExecutionCoordinator.name)",
    )


def main() -> None:
    shutil.rmtree(REPORTS, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(REBUILD, ignore_errors=True)
    shutil.rmtree(CORE_DIST, ignore_errors=True)
    REPORTS.mkdir(parents=True)
    DIST.mkdir(parents=True)
    REBUILD.mkdir(parents=True)
    CORE_DIST.mkdir(parents=True)

    run(
        sys.executable,
        "-m",
        "compileall",
        "-q",
        str(MODULE / "src"),
        str(MODULE / "tests"),
        str(ROOT / "scripts" / "normalize_sdist.py"),
        str(ROOT / "scripts" / "paper_execution_gate.py"),
        str(Path(__file__)),
    )
    run(
        sys.executable,
        "-m",
        "ruff",
        "check",
        str(MODULE / "src"),
        str(MODULE / "tests"),
        str(ROOT / "scripts" / "normalize_sdist.py"),
        str(ROOT / "scripts" / "paper_execution_gate.py"),
        str(Path(__file__)),
    )
    run(
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(MODULE / "pyproject.toml"),
        str(MODULE / "src" / "paper_execution"),
        str(ROOT / "scripts" / "normalize_sdist.py"),
        str(Path(__file__)),
    )
    run(
        sys.executable,
        "-c",
        "from scripts.paper_execution_gate import _network_security_scan; _network_security_scan()",
    )
    run(sys.executable, "-m", "bandit", "-q", "-r", str(MODULE / "src"))

    run(sys.executable, "-m", "coverage", "erase")
    run(
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--branch",
        "--source=paper_execution",
        "-m",
        "pytest",
        str(MODULE / "tests"),
        f"--junitxml={REPORTS / 'tests.xml'}",
    )
    sanitize_junit_report(REPORTS / "tests.xml")
    run(
        sys.executable,
        "-m",
        "coverage",
        "report",
        "--precision=2",
        "--fail-under=85",
    )
    run(
        sys.executable,
        "-m",
        "coverage",
        "json",
        "-o",
        str(REPORTS / "coverage.json"),
    )

    run(sys.executable, "-m", "build", str(MODULE), "--outdir", str(DIST))
    run(
        sys.executable,
        str(ROOT / "scripts" / "normalize_sdist.py"),
        *glob.glob(str(DIST / "*.tar.gz")),
    )
    run(sys.executable, "-m", "twine", "check", *glob.glob(str(DIST / "*")))
    run(sys.executable, "-m", "build", str(MODULE), "--outdir", str(REBUILD))
    run(
        sys.executable,
        str(ROOT / "scripts" / "normalize_sdist.py"),
        *glob.glob(str(REBUILD / "*.tar.gz")),
    )
    run(sys.executable, "-m", "twine", "check", *glob.glob(str(REBUILD / "*")))
    first = digest_files(DIST)
    second = digest_files(REBUILD)
    if first != second:
        raise RuntimeError(f"non-reproducible Paper artifacts: {first!r} != {second!r}")
    (REPORTS / "artifact-sha256.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(first.items())),
        encoding="utf-8",
    )

    run(sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(CORE_DIST))
    clean_wheel_smoke()


if __name__ == "__main__":
    main()
