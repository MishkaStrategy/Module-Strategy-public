from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
from collections.abc import Iterator

from run_release_gate import ROOT
from run_release_gate import SOURCE_ROOTS
from run_release_gate import ReleaseGate as BaseReleaseGate
from run_release_gate import _remove_build_products
from run_release_gate import _run
from run_release_gate import _smoke_code
from run_release_gate import _snapshot_changes
from run_release_gate import _source_snapshot
from run_release_gate import _venv_executable
from run_release_gate import _venv_python

_TEMP_PATTERNS = (
    "fvg-release-*",
    "fvg-dist-*",
    "fvg-clean-wheel-*",
    "fvg-clean-sdist-*",
)
_MIN_FREE_BYTES = 256 * 1024 * 1024
_CACHE_PRUNE_BELOW_BYTES = 768 * 1024 * 1024
_FROZEN_VALIDATION_BASE_SHA = "5e03b933207a99940244634f49a756d27888a5c3"  # pragma: allowlist secret


@contextmanager
def _uv_cache_enabled() -> Iterator[None]:
    previous = os.environ.pop("UV_NO_CACHE", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["UV_NO_CACHE"] = previous


def _event_base_sha() -> str:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH is unavailable")
    payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    try:
        value = payload["pull_request"]["base"]["sha"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("GitHub event payload is missing pull_request.base.sha") from error
    if not isinstance(value, str):
        raise RuntimeError("pull_request.base.sha must be a string")
    return value


class PortableReleaseGate(BaseReleaseGate):
    def prepare(self) -> None:
        self.evidence.mkdir(parents=True, exist_ok=True)
        temp_root = self.venv.parent
        temp_root.mkdir(parents=True, exist_ok=True)
        keep = {self.venv, self.dist_root, self.clean_wheel, self.clean_sdist}
        before = shutil.disk_usage(temp_root)
        removed: list[str] = []

        for pattern in _TEMP_PATTERNS:
            for path in temp_root.glob(pattern):
                if path in keep:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed.append(str(path))

        after_cleanup = shutil.disk_usage(temp_root)
        with (self.evidence / "storage-cleanup.log").open("w", encoding="utf-8") as log:
            log.write(f"temp_root={temp_root}\n")
            log.write(f"free_before={before.free}\n")
            log.write(f"free_after_directory_cleanup={after_cleanup.free}\n")
            for path in removed:
                log.write(f"removed={path}\n")
            if after_cleanup.free < _CACHE_PRUNE_BELOW_BYTES:
                with _uv_cache_enabled():
                    _run(["uv", "cache", "prune", "--ci"], log=log)
            final = shutil.disk_usage(temp_root)
            log.write(f"free_after_cache_cleanup={final.free}\n")

        if final.free < _MIN_FREE_BYTES:
            raise RuntimeError(f"insufficient runner temp capacity after bounded cleanup: {final.free} bytes")

        pinned_core = os.environ.get("CORE_SHA")
        if not pinned_core:
            raise RuntimeError("CORE_SHA is unavailable")
        event_base = _event_base_sha()
        validation_mode = event_base == _FROZEN_VALIDATION_BASE_SHA
        if event_base != pinned_core and not validation_mode:
            raise RuntimeError(
                f"event base {event_base!r} is neither pinned Core {pinned_core} "
                f"nor frozen validation base {_FROZEN_VALIDATION_BASE_SHA}"
            )

        if validation_mode:
            os.environ["CORE_SHA"] = event_base
        try:
            super().prepare()
        finally:
            os.environ["CORE_SHA"] = pinned_core

        (self.evidence / "core-head.txt").write_text(
            f"{pinned_core}\n",
            encoding="utf-8",
        )
        if validation_mode:
            (self.evidence / "validation-base.txt").write_text(
                f"{event_base}\n",
                encoding="utf-8",
            )

    def _install(self, arguments: list[str], log_name: str) -> None:
        if arguments == ["-e", "./plugins/fvg[dev]"]:
            arguments = ["./plugins/fvg[dev]"]
        python = self.python
        if python is None:
            raise RuntimeError("release environment Python is not initialized")
        with (self.evidence / log_name).open("w", encoding="utf-8") as log:
            with _uv_cache_enabled():
                _run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        "--link-mode",
                        "hardlink",
                        *arguments,
                    ],
                    log=log,
                )

    def _create_clean_environment(
        self,
        path: Path,
        artifact: Path,
        log: object,
    ) -> Path:
        shutil.rmtree(path, ignore_errors=True)
        _run(
            ["uv", "venv", "--python", self.python_version, str(path)],
            log=log,
        )
        python = _venv_python(path)
        with _uv_cache_enabled():
            for requirement in (self.core_path, artifact):
                _run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        "--link-mode",
                        "hardlink",
                        str(requirement),
                    ],
                    log=log,
                )
            _run(["uv", "pip", "check", "--python", str(python)], log=log)
        _run([str(python), "-c", _smoke_code()], log=log)
        return python

    def gate_canonicalize(self, log: object) -> None:
        _remove_build_products()
        ruff = _venv_executable(self.venv, "ruff")
        paths = [str(path.relative_to(ROOT)) for path in SOURCE_ROOTS]
        _run([str(ruff), "format", "--check", *paths], log=log)
        _run([str(ruff), "check", *paths], log=log)
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

    def gate_coverage(self, log: object) -> None:
        coverage = _venv_executable(self.venv, "coverage")
        plugin_root = ROOT / "plugins/fvg"
        env = {"COVERAGE_FILE": str(self.evidence / ".coverage")}
        _run([str(coverage), "erase"], log=log, cwd=plugin_root, env=env)
        _run(
            [
                str(coverage),
                "run",
                "--branch",
                "--source=modular_strategy_fvg",
                "-m",
                "pytest",
                "-q",
                "tests",
            ],
            log=log,
            cwd=plugin_root,
            env=env,
        )
        with (self.evidence / "coverage-report.txt").open("w", encoding="utf-8") as report:
            _run(
                [str(coverage), "report", "--show-missing", "--fail-under=95"],
                log=report,
                cwd=plugin_root,
                env=env,
            )
        _run(
            [str(coverage), "json", "-o", str(self.evidence / "coverage.json")],
            log=log,
            cwd=plugin_root,
            env=env,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the portable strict FVG 0.3.0 release gate")
    parser.add_argument("expected_sha")
    parser.add_argument("core_path", type=Path)
    parser.add_argument("python_version")
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args()
    gate = PortableReleaseGate(
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
