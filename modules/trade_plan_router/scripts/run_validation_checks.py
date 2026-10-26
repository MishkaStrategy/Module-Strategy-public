from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


REPORT_DIR = Path("reports/trade_plan_router")
JUNIT_PATH = REPORT_DIR / "validation-tests.xml"
COVERAGE_JSON_PATH = REPORT_DIR / "coverage.json"
CODES_PATH = REPORT_DIR / "CHECK_CODES.json"
STATUS_PATH = REPORT_DIR / "VALIDATION_GATE.txt"
COVERAGE_MINIMUM = 90.0


def _run(command: list[str], output_path: Path) -> int:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(completed.stdout, encoding="utf-8")
    print(f"$ {' '.join(command)}")
    print(completed.stdout, end="")
    print(f"exit_code={completed.returncode}")
    return completed.returncode


def _parse_junit() -> tuple[int, int, int, int, float]:
    if not JUNIT_PATH.is_file():
        return 0, 0, 1, 0, 0.0
    root = ET.parse(JUNIT_PATH).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        return 0, 0, 1, 0, 0.0
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    duration = sum(float(suite.attrib.get("time", "0")) for suite in suites)
    return tests, failures, errors, skipped, duration


def _coverage_totals() -> tuple[float, int, int, int, int]:
    if not COVERAGE_JSON_PATH.is_file():
        return 0.0, 0, 0, 0, 0
    payload = json.loads(COVERAGE_JSON_PATH.read_text(encoding="utf-8"))
    totals = payload["totals"]
    return (
        float(totals["percent_covered"]),
        int(totals["covered_lines"]),
        int(totals["missing_lines"]),
        int(totals.get("covered_branches", 0)),
        int(totals.get("missing_branches", 0)),
    )


def _write_status(codes: dict[str, int]) -> bool:
    tests, failures, errors, skipped, duration = _parse_junit()
    (
        coverage_percent,
        covered_lines,
        missing_lines,
        covered_branches,
        missing_branches,
    ) = _coverage_totals()
    coverage_passed = coverage_percent >= COVERAGE_MINIMUM
    passed = (
        all(code == 0 for code in codes.values())
        and tests > 0
        and failures == 0
        and errors == 0
        and coverage_passed
    )
    status = "passed" if passed else "failed"
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    head_sha = os.environ.get("PR_HEAD_SHA", "local")
    STATUS_PATH.write_text(
        "\n".join(
            (
                f"run_id={run_id}",
                f"head_sha={head_sha}",
                f"status={status}",
                f"pytest_exit_code={codes['pytest']}",
                f"ruff_exit_code={codes['ruff']}",
                f"mypy_exit_code={codes['mypy']}",
                f"coverage_report_exit_code={codes['coverage_report']}",
                f"coverage_json_exit_code={codes['coverage_json']}",
                f"tests={tests}",
                f"failures={failures}",
                f"errors={errors}",
                f"skipped={skipped}",
                f"duration_seconds={duration:.6f}",
                f"coverage_minimum={COVERAGE_MINIMUM:.2f}",
                f"coverage_percent={coverage_percent:.4f}",
                f"coverage_passed={str(coverage_passed).lower()}",
                f"covered_lines={covered_lines}",
                f"missing_lines={missing_lines}",
                f"covered_branches={covered_branches}",
                f"missing_branches={missing_branches}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    CODES_PATH.write_text(
        json.dumps({**codes, "passed": passed}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(STATUS_PATH.read_text(encoding="utf-8"), end="")
    return passed


def run_checks() -> int:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "STATIC_FIX_DIAGNOSTIC.txt").unlink(missing_ok=True)
    JUNIT_PATH.unlink(missing_ok=True)
    COVERAGE_JSON_PATH.unlink(missing_ok=True)

    python = sys.executable
    codes: dict[str, int] = {}
    codes["ruff"] = _run(
        [
            python,
            "-m",
            "ruff",
            "check",
            "modules/trade_plan_router/src",
            "modules/trade_plan_router/tests",
        ],
        REPORT_DIR / "ruff.txt",
    )
    codes["mypy"] = _run(
        [python, "-m", "mypy", "modules/trade_plan_router/src"],
        REPORT_DIR / "mypy.txt",
    )
    subprocess.run([python, "-m", "coverage", "erase"], check=False)
    codes["pytest"] = _run(
        [
            python,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--source=modules/trade_plan_router/src/modular_strategy_trade_plan_router",
            "-m",
            "pytest",
            "modules/trade_plan_router/tests",
            "-q",
            f"--junitxml={JUNIT_PATH.as_posix()}",
        ],
        REPORT_DIR / "pytest.txt",
    )
    codes["coverage_report"] = _run(
        [python, "-m", "coverage", "report", "-m"],
        REPORT_DIR / "coverage.txt",
    )
    codes["coverage_json"] = subprocess.run(
        [python, "-m", "coverage", "json", "-o", str(COVERAGE_JSON_PATH)],
        check=False,
    ).returncode
    _write_status(codes)
    return 0


def enforce() -> int:
    if not CODES_PATH.is_file():
        print("validation code report is missing", file=sys.stderr)
        return 2
    payload = json.loads(CODES_PATH.read_text(encoding="utf-8"))
    if payload.get("passed") is True:
        return 0
    print(STATUS_PATH.read_text(encoding="utf-8"), file=sys.stderr, end="")
    return 1


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--enforce":
        return enforce()
    if len(sys.argv) != 1:
        print("usage: run_validation_checks.py [--enforce]", file=sys.stderr)
        return 2
    return run_checks()


if __name__ == "__main__":
    raise SystemExit(main())
