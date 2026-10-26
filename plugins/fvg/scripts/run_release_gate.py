from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import traceback
import zipfile

ROOT = Path.cwd().resolve()
SOURCE_ROOTS = (
    ROOT / "plugins/fvg/src",
    ROOT / "plugins/fvg/tests",
    ROOT / "plugins/fvg/benchmarks",
    ROOT / "plugins/fvg/scripts",
)
IGNORED_PARTS = {
    "__pycache__",
    ".coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}
FIXED_SOURCE_DATE_EPOCH = "946684800"
GATE_NAMES = (
    "canonicalize",
    "source_clean",
    "compileall",
    "ruff_format",
    "ruff_lint",
    "mypy",
    "runtime_tests",
    "full_tests",
    "coverage",
    "optimized_tests",
    "benchmark",
    "packaging",
    "clean_install",
    "bandit",
    "secrets",
    "pip_audit",
    "sbom",
)


def _run(
    command: list[str],
    *,
    log: object,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    process = subprocess.run(
        command,
        cwd=cwd,
        env=merged_env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


def _venv_python(venv: Path) -> Path:
    candidates = (venv / "Scripts/python.exe", venv / "bin/python")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"virtual environment Python is missing: {venv}")


def _venv_executable(venv: Path, name: str) -> Path:
    candidates = (
        venv / "Scripts" / f"{name}.exe",
        venv / "Scripts" / name,
        venv / "bin" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"virtual environment executable is missing: {name}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_source_files() -> Iterable[Path]:
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            raise RuntimeError(f"source root is missing: {root}")
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            yield path


def _source_snapshot() -> dict[str, str]:
    return {path.relative_to(ROOT).as_posix(): _sha256_file(path) for path in _iter_source_files()}


def _snapshot_changes(
    before: dict[str, str],
    after: dict[str, str],
) -> dict[str, dict[str, str | None]]:
    return {
        path: {"before": before.get(path), "after": after.get(path)}
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path)
    }


def _reset_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _remove_build_products() -> None:
    for path in (ROOT / "plugins/fvg/dist", ROOT / "plugins/fvg/build"):
        shutil.rmtree(path, ignore_errors=True)
    for path in (ROOT / "plugins/fvg/src").glob("*.egg-info"):
        shutil.rmtree(path, ignore_errors=True)


def _event_payload() -> dict[str, object]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH is unavailable")
    payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub event payload must be a mapping")
    return payload


def _nested_mapping(payload: object, *keys: str) -> dict[str, object]:
    current = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise RuntimeError(f"GitHub event payload is missing {'.'.join(keys)}")
        current = current[key]
    if not isinstance(current, dict):
        raise RuntimeError(f"GitHub event field {'.'.join(keys)} must be a mapping")
    return current


def _smoke_code() -> str:
    return """
from importlib.metadata import entry_points, version
from importlib.resources import files
from modular_strategy_fvg import FvgModule

assert version("modular-strategy-fvg") == "0.3.0"
assert files("modular_strategy_fvg").joinpath("py.typed").is_file()
eps = {ep.name: ep for ep in entry_points().select(group="modular_strategy.modules")}
assert eps["fvg"].load() is FvgModule
state = FvgModule(timeframes=("15m",)).snapshot_state()
restored = FvgModule(timeframes=("15m",))
restored.restore_state(state)
assert restored.snapshot_state() == state
"""


class ReleaseGate:
    def __init__(
        self,
        *,
        expected_sha: str,
        core_path: Path,
        python_version: str,
        evidence: Path,
    ) -> None:
        self.expected_sha = expected_sha
        self.core_path = core_path.resolve()
        self.python_version = python_version
        self.evidence = evidence.resolve()
        suffix = f"{os.environ.get('GITHUB_RUN_ID', 'manual')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        runner_temp = Path(os.environ.get("RUNNER_TEMP", str(ROOT / ".tmp"))).resolve()
        self.venv = runner_temp / f"fvg-release-{python_version}-{suffix}"
        self.dist_root = runner_temp / f"fvg-dist-{python_version}-{suffix}"
        self.clean_wheel = runner_temp / f"fvg-clean-wheel-{python_version}-{suffix}"
        self.clean_sdist = runner_temp / f"fvg-clean-sdist-{python_version}-{suffix}"
        self.outcomes: list[tuple[str, str]] = []
        self.python: Path | None = None
        self.initial_snapshot: dict[str, str] = {}
        self.canonical_changes: dict[str, dict[str, str | None]] = {}

    def prepare(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.expected_sha):
            raise ValueError("expected SHA must be a lowercase 40-character commit SHA")
        if not self.core_path.is_dir():
            raise RuntimeError(f"Core checkout is missing: {self.core_path}")

        payload = _event_payload()
        pull_request = _nested_mapping(payload, "pull_request")
        head = _nested_mapping(pull_request, "head")
        base = _nested_mapping(pull_request, "base")
        event_head = head.get("sha")
        event_base = base.get("sha")
        expected_core = os.environ.get("CORE_SHA")
        if event_head != self.expected_sha:
            raise RuntimeError(f"event head {event_head!r} does not match {self.expected_sha}")
        if not expected_core or not re.fullmatch(r"[0-9a-f]{40}", expected_core):
            raise RuntimeError("CORE_SHA must be a lowercase 40-character commit SHA")
        if event_base != expected_core:
            raise RuntimeError(f"event base {event_base!r} does not match pinned Core {expected_core}")

        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "exact-head.txt").write_text(
            f"{self.expected_sha}\n",
            encoding="utf-8",
        )
        (self.evidence / "core-head.txt").write_text(
            f"{expected_core}\n",
            encoding="utf-8",
        )
        (self.evidence / "requested-python.txt").write_text(
            f"{self.python_version}\n",
            encoding="utf-8",
        )
        self.initial_snapshot = _source_snapshot()
        (self.evidence / "source-snapshot-before.json").write_text(
            json.dumps(self.initial_snapshot, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        for path in (self.venv, self.dist_root, self.clean_wheel, self.clean_sdist):
            shutil.rmtree(path, ignore_errors=True)
        self.dist_root.mkdir(parents=True, exist_ok=True)
        with (self.evidence / "environment-setup.log").open("w", encoding="utf-8") as log:
            _run(["uv", "python", "install", self.python_version], log=log)
            _run(
                [
                    "uv",
                    "venv",
                    "--python",
                    self.python_version,
                    "--seed",
                    str(self.venv),
                ],
                log=log,
            )
        self.python = _venv_python(self.venv)
        self._install([str(self.core_path)], "install-core.log")
        self._install(["-e", "./plugins/fvg[dev]"], "install-fvg.log")
        self._install(
            [
                "setuptools>=68",
                "wheel",
                "build",
                "twine",
                "bandit",
                "detect-secrets",
                "pip-audit",
                "cyclonedx-bom",
                "ruff==0.12.0",
            ],
            "install-release-tools.log",
        )
        with (self.evidence / "pip-check.log").open("w", encoding="utf-8") as log:
            _run([str(self.python), "-m", "pip", "check"], log=log)
        with (self.evidence / "python-version.txt").open("w", encoding="utf-8") as log:
            _run([str(self.python), "--version"], log=log)
        with (self.evidence / "toolchain-freeze.txt").open("w", encoding="utf-8") as log:
            _run([str(self.python), "-m", "pip", "freeze", "--all"], log=log)

    def _install(self, arguments: list[str], log_name: str) -> None:
        assert self.python is not None
        with (self.evidence / log_name).open("w", encoding="utf-8") as log:
            _run([str(self.python), "-m", "pip", "install", *arguments], log=log)

    def cleanup(self) -> None:
        for path in (self.venv, self.dist_root, self.clean_wheel, self.clean_sdist):
            shutil.rmtree(path, ignore_errors=True)

    def run_gate(self, name: str, function: Callable[[object], None]) -> None:
        with (self.evidence / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                function(log)
            except Exception:
                traceback.print_exc(file=log)
                self.outcomes.append((name, "failure"))
            else:
                self.outcomes.append((name, "success"))
        self._write_outcomes()

    def _write_outcomes(self) -> None:
        payload = "".join(f"{name}={outcome}\n" for name, outcome in self.outcomes)
        (self.evidence / "outcomes.txt").write_text(payload, encoding="utf-8")

    def gate_canonicalize(self, log: object) -> None:
        ruff = _venv_executable(self.venv, "ruff")
        paths = [str(path.relative_to(ROOT)) for path in SOURCE_ROOTS]
        _run([str(ruff), "format", *paths], log=log)
        _run([str(ruff), "check", "--fix", *paths], log=log)
        after = _source_snapshot()
        self.canonical_changes = _snapshot_changes(self.initial_snapshot, after)
        (self.evidence / "source-snapshot-after.json").write_text(
            json.dumps(after, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (self.evidence / "canonicalization.patch").write_text(
            json.dumps(self.canonical_changes, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def gate_source_clean(self, log: object) -> None:
        if self.canonical_changes:
            changed = ", ".join(self.canonical_changes)
            raise RuntimeError(f"canonicalization changed committed source: {changed}")
        log.write("source snapshot remained byte-identical after canonicalization\n")

    def gate_compileall(self, log: object) -> None:
        assert self.python is not None
        _run(
            [
                str(self.python),
                "-m",
                "compileall",
                "-q",
                "plugins/fvg/src",
                "plugins/fvg/tests",
                "plugins/fvg/benchmarks",
                "plugins/fvg/scripts",
            ],
            log=log,
        )

    def gate_ruff_format(self, log: object) -> None:
        ruff = _venv_executable(self.venv, "ruff")
        _run(
            [
                str(ruff),
                "format",
                "--check",
                "plugins/fvg/src",
                "plugins/fvg/tests",
                "plugins/fvg/benchmarks",
                "plugins/fvg/scripts",
            ],
            log=log,
        )

    def gate_ruff_lint(self, log: object) -> None:
        ruff = _venv_executable(self.venv, "ruff")
        _run(
            [
                str(ruff),
                "check",
                "plugins/fvg/src",
                "plugins/fvg/tests",
                "plugins/fvg/benchmarks",
                "plugins/fvg/scripts",
            ],
            log=log,
        )

    def gate_mypy(self, log: object) -> None:
        mypy = _venv_executable(self.venv, "mypy")
        _run([str(mypy), "--strict", "plugins/fvg/src/modular_strategy_fvg"], log=log)

    def gate_runtime_tests(self, log: object) -> None:
        assert self.python is not None
        _run(
            [
                str(self.python),
                "-m",
                "pytest",
                "-q",
                "plugins/fvg/tests/unit/test_runtime_v030.py",
                f"--junitxml={self.evidence / 'runtime-tests.xml'}",
            ],
            log=log,
        )

    def gate_full_tests(self, log: object) -> None:
        assert self.python is not None
        _run(
            [
                str(self.python),
                "-m",
                "pytest",
                "-q",
                "plugins/fvg/tests",
                f"--junitxml={self.evidence / 'full-tests.xml'}",
            ],
            log=log,
        )

    def gate_coverage(self, log: object) -> None:
        coverage = _venv_executable(self.venv, "coverage")
        env = {"COVERAGE_FILE": str(self.evidence / ".coverage")}
        _run([str(coverage), "erase"], log=log, env=env)
        _run(
            [
                str(coverage),
                "run",
                "--branch",
                "--source=modular_strategy_fvg",
                "-m",
                "pytest",
                "-q",
                "plugins/fvg/tests",
            ],
            log=log,
            env=env,
        )
        with (self.evidence / "coverage-report.txt").open("w", encoding="utf-8") as report:
            _run(
                [str(coverage), "report", "--show-missing", "--fail-under=95"],
                log=report,
                env=env,
            )
        _run(
            [str(coverage), "json", "-o", str(self.evidence / "coverage.json")],
            log=log,
            env=env,
        )

    def gate_optimized_tests(self, log: object) -> None:
        assert self.python is not None
        _run(
            [str(self.python), "-O", "-m", "pytest", "-q", "plugins/fvg/tests"],
            log=log,
        )

    def gate_benchmark(self, log: object) -> None:
        assert self.python is not None
        output = self.evidence / "benchmark.json"
        _run(
            [
                str(self.python),
                "plugins/fvg/benchmarks/run_benchmarks.py",
                "--ci",
                "--output",
                str(output),
            ],
            log=log,
        )
        report = json.loads(output.read_text(encoding="utf-8"))
        if report.get("module_version") != "0.3.0":
            raise RuntimeError("benchmark used an unexpected FVG version")
        restart = report.get("restart_midpoint")
        if not isinstance(restart, dict) or restart.get("digests_match") is not True:
            raise RuntimeError("restart benchmark digest mismatch")
        memory = report.get("memory")
        if not isinstance(memory, dict) or not isinstance(memory.get("peak_memory_bytes"), int):
            raise RuntimeError("benchmark memory evidence is missing")
        extended = report.get("extended")
        throughput = extended.get("throughput_calls_per_second") if isinstance(extended, dict) else None
        if not isinstance(throughput, dict) or float(throughput.get("minimum", 0)) <= 0:
            raise RuntimeError("benchmark throughput evidence is invalid")

    def gate_packaging(self, log: object) -> None:
        assert self.python is not None
        for name in ("raw1", "raw2", "norm1", "norm2"):
            (self.dist_root / name).mkdir(parents=True, exist_ok=True)
        for build_index in (1, 2):
            _remove_build_products()
            env = {
                "SOURCE_DATE_EPOCH": FIXED_SOURCE_DATE_EPOCH,
                "PYTHONHASHSEED": str(build_index),
            }
            _run(
                [
                    str(self.python),
                    "-m",
                    "build",
                    "--outdir",
                    str(self.dist_root / f"raw{build_index}"),
                    "plugins/fvg",
                ],
                log=log,
                env=env,
            )
            raw_dir = self.dist_root / f"raw{build_index}"
            wheels = tuple(raw_dir.glob("*.whl"))
            sdists = tuple(raw_dir.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise RuntimeError("build did not produce exactly one wheel and one sdist")
            norm_dir = self.dist_root / f"norm{build_index}"
            _run(
                [
                    str(self.python),
                    "plugins/fvg/scripts/normalize_wheel.py",
                    "--epoch",
                    FIXED_SOURCE_DATE_EPOCH,
                    str(wheels[0]),
                    str(norm_dir / "modular_strategy_fvg-0.3.0-py3-none-any.whl"),
                ],
                log=log,
            )
            _run(
                [
                    str(self.python),
                    "plugins/fvg/scripts/normalize_sdist.py",
                    "--epoch",
                    FIXED_SOURCE_DATE_EPOCH,
                    str(sdists[0]),
                    str(norm_dir / "modular_strategy_fvg-0.3.0.tar.gz"),
                ],
                log=log,
            )

        first_wheel = self.dist_root / "norm1/modular_strategy_fvg-0.3.0-py3-none-any.whl"
        second_wheel = self.dist_root / "norm2/modular_strategy_fvg-0.3.0-py3-none-any.whl"
        first_sdist = self.dist_root / "norm1/modular_strategy_fvg-0.3.0.tar.gz"
        second_sdist = self.dist_root / "norm2/modular_strategy_fvg-0.3.0.tar.gz"
        if first_wheel.read_bytes() != second_wheel.read_bytes():
            raise RuntimeError("normalized wheels are not reproducible")
        if first_sdist.read_bytes() != second_sdist.read_bytes():
            raise RuntimeError("normalized sdists are not reproducible")

        twine = _venv_executable(self.venv, "twine")
        with (self.evidence / "twine-check.txt").open("w", encoding="utf-8") as report:
            _run([str(twine), "check", str(first_wheel), str(first_sdist)], log=report)
        with zipfile.ZipFile(first_wheel) as archive:
            bad_member = archive.testzip()
        if bad_member is not None:
            raise RuntimeError(f"wheel archive member is corrupt: {bad_member}")
        (self.evidence / "wheel-archive-check.txt").write_text("ok\n", encoding="utf-8")
        with tarfile.open(first_sdist, "r:gz") as archive:
            members = "\n".join(member.name for member in archive.getmembers())
        (self.evidence / "sdist-members.txt").write_text(f"{members}\n", encoding="utf-8")
        distributions = self.evidence / "distributions"
        _reset_directory(distributions)
        shutil.copy2(first_wheel, distributions / first_wheel.name)
        shutil.copy2(first_sdist, distributions / first_sdist.name)
        checksums = (
            f"{_sha256_file(first_sdist)}  {first_sdist.name}\n{_sha256_file(first_wheel)}  {first_wheel.name}\n"
        )
        (self.evidence / "distribution-checksums.sha256").write_text(
            checksums,
            encoding="utf-8",
        )

    def _create_clean_environment(
        self,
        path: Path,
        artifact: Path,
        log: object,
    ) -> Path:
        shutil.rmtree(path, ignore_errors=True)
        _run(
            ["uv", "venv", "--python", self.python_version, "--seed", str(path)],
            log=log,
        )
        python = _venv_python(path)
        _run([str(python), "-m", "pip", "install", str(self.core_path)], log=log)
        _run([str(python), "-m", "pip", "install", str(artifact)], log=log)
        _run([str(python), "-m", "pip", "check"], log=log)
        _run([str(python), "-c", _smoke_code()], log=log)
        return python

    def gate_clean_install(self, log: object) -> None:
        wheel = self.dist_root / "norm1/modular_strategy_fvg-0.3.0-py3-none-any.whl"
        sdist = self.dist_root / "norm1/modular_strategy_fvg-0.3.0.tar.gz"
        if not wheel.is_file() or not sdist.is_file():
            raise RuntimeError("packaging artifacts are unavailable")
        self._create_clean_environment(self.clean_wheel, wheel, log)
        self._create_clean_environment(self.clean_sdist, sdist, log)

    def gate_bandit(self, log: object) -> None:
        bandit = _venv_executable(self.venv, "bandit")
        excluded = ",".join(
            (
                "plugins/fvg/scripts/run_release_gate.py",
                "plugins/fvg/scripts/materialize_format.py",
            )
        )
        _run(
            [
                str(bandit),
                "-q",
                "-r",
                "plugins/fvg/src",
                "plugins/fvg/scripts",
                "-x",
                excluded,
                "-f",
                "json",
                "-o",
                str(self.evidence / "bandit.json"),
            ],
            log=log,
        )

    def gate_secrets(self, log: object) -> None:
        scanner = _venv_executable(self.venv, "detect-secrets")
        output = self.evidence / "detect-secrets.json"
        command = [
            str(scanner),
            "scan",
            "--all-files",
            "--no-verify",
            "plugins/fvg/src",
            "plugins/fvg/scripts",
            "plugins/fvg/pyproject.toml",
            "plugins/fvg/setup.cfg",
            ".github/workflows/ci.yml",
            ".github/workflows/fvg-module-ci.yml",
            ".github/workflows/fvg-release-validation.yml",
        ]
        with output.open("w", encoding="utf-8") as report:
            _run(command, log=report)
        findings = json.loads(output.read_text(encoding="utf-8")).get("results")
        if not isinstance(findings, dict):
            raise RuntimeError("detect-secrets report is missing a results mapping")
        non_empty = {path: rows for path, rows in findings.items() if rows}
        if non_empty:
            raise RuntimeError(f"detect-secrets findings require review: {sorted(non_empty)}")

    def gate_pip_audit(self, log: object) -> None:
        clean_python = _venv_python(self.clean_wheel)
        site_path = subprocess.check_output(
            [
                str(clean_python),
                "-c",
                "import site; print(site.getsitepackages()[0])",
            ],
            text=True,
        ).strip()
        auditor = _venv_executable(self.venv, "pip-audit")
        _run(
            [
                str(auditor),
                "--path",
                site_path,
                "--format",
                "json",
                "--output",
                str(self.evidence / "pip-audit.json"),
                "--progress-spinner",
                "off",
            ],
            log=log,
        )

    def gate_sbom(self, log: object) -> None:
        clean_python = _venv_python(self.clean_wheel)
        cyclonedx = _venv_executable(self.venv, "cyclonedx-py")
        _run(
            [
                str(cyclonedx),
                "environment",
                str(clean_python),
                "--output-reproducible",
                "--spec-version",
                "1.6",
                "--output-format",
                "JSON",
                "--output-file",
                str(self.evidence / "sbom.cdx.json"),
            ],
            log=log,
        )

    def execute(self) -> int:
        self.prepare()
        gates: dict[str, Callable[[object], None]] = {
            "canonicalize": self.gate_canonicalize,
            "source_clean": self.gate_source_clean,
            "compileall": self.gate_compileall,
            "ruff_format": self.gate_ruff_format,
            "ruff_lint": self.gate_ruff_lint,
            "mypy": self.gate_mypy,
            "runtime_tests": self.gate_runtime_tests,
            "full_tests": self.gate_full_tests,
            "coverage": self.gate_coverage,
            "optimized_tests": self.gate_optimized_tests,
            "benchmark": self.gate_benchmark,
            "packaging": self.gate_packaging,
            "clean_install": self.gate_clean_install,
            "bandit": self.gate_bandit,
            "secrets": self.gate_secrets,
            "pip_audit": self.gate_pip_audit,
            "sbom": self.gate_sbom,
        }
        for name in GATE_NAMES:
            self.run_gate(name, gates[name])
        success_count = sum(outcome == "success" for _, outcome in self.outcomes)
        (self.evidence / "summary.txt").write_text(
            f"gates={len(self.outcomes)}\nsuccesses={success_count}\n",
            encoding="utf-8",
        )
        return 0 if len(self.outcomes) == 17 and success_count == 17 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the strict cross-platform FVG 0.3.0 release gate")
    parser.add_argument("expected_sha")
    parser.add_argument("core_path", type=Path)
    parser.add_argument("python_version")
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args()
    gate = ReleaseGate(
        expected_sha=args.expected_sha,
        core_path=args.core_path,
        python_version=args.python_version,
        evidence=args.evidence_dir,
    )
    try:
        return gate.execute()
    finally:
        gate.cleanup()


if __name__ == "__main__":
    sys.exit(main())
