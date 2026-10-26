from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

LOCAL_DEVELOPMENT = "LOCAL DEVELOPMENT — NOT READY"
LOCAL_VALIDATION = "LOCAL VALIDATION IN PROGRESS"
LOCAL_RELEASE_CANDIDATE = "LOCAL RELEASE CANDIDATE — READY FOR CONTROLLED INTEGRATION"


def _read(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"required evidence file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON evidence: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON evidence root must be an object: {path}")
    return value


def _require_contains(path: Path, expected: str, passed: list[str]) -> None:
    text = _read(path)
    if expected not in text:
        raise ValueError(f"evidence {path} does not contain {expected!r}")
    passed.append(path.name)


def _external_report(path: Path | None, *, label: str) -> tuple[bool, str]:
    if path is None:
        return False, f"{label}: report unavailable"
    report = _json(path)
    if report.get("status") != "PASS":
        return False, f"{label}: report is not PASS"
    return True, f"{label}: PASS"


def evaluate_release_gate(
    *,
    fast_ci_dir: Path,
    round_name: str,
    performance_report: Path,
    risk_fuzz_report: Path,
    serialization_fuzz_report: Path,
    stress_report: Path,
    verified_checkout: bool = False,
    mac_heavy_audit_report: Path | None = None,
    strict_type_report: Path | None = None,
    external_security_report: Path | None = None,
    dependency_audit_report: Path | None = None,
    mutation_report: Path | None = None,
) -> dict[str, Any]:
    passed: list[str] = []
    blockers: list[str] = []
    failures: list[str] = []

    required_text = {
        f"result-{round_name}.txt": "PASS_WITH_EXPLICIT_OPTIONAL_TOOLING_BLOCKERS",
        f"tests-{round_name}.txt": "passed",
        f"coverage-{round_name}.txt": "TOTAL",
        f"static-security-{round_name}.txt": "security_scan: PASS",
        f"type-surface-{round_name}.txt": "type_surface_audit: PASS",
        f"golden-matrix-{round_name}.txt": "PASS (30 fixtures)",
        f"deterministic-replay-{round_name}.txt": "deterministic_replay: PASS",
        f"reproducible-wheels-{round_name}.txt": "reproducible_wheels: PASS",
        f"wheel-policy-{round_name}.txt": "wheel_content_policy: PASS",
        f"wheel-import-{round_name}.txt": "clean_wheel_import: PASS",
        f"wheel-uninstall-{round_name}.txt": "clean_wheel_uninstall: PASS",
        f"wheel-reinstall-{round_name}.txt": "clean_wheel_reinstall: PASS",
        f"pip-check-reinstall-{round_name}.txt": "No broken requirements found.",
        f"package-metadata-{round_name}.txt": "source_package_metadata: PASS",
        f"path-collisions-{round_name}.txt": "path_collision_check: PASS",
    }
    for filename, expected in required_text.items():
        try:
            _require_contains(fast_ci_dir / filename, expected, passed)
        except ValueError as exc:
            failures.append(str(exc))

    coverage_path = fast_ci_dir / f"coverage-{round_name}.txt"
    if coverage_path.is_file():
        match = re.search(r"^TOTAL\s+.+?\s+(\d+)%\s*$", _read(coverage_path), re.MULTILINE)
        if match is None:
            failures.append("coverage TOTAL percentage cannot be parsed")
        elif int(match.group(1)) < 90:
            failures.append(f"branch-aware coverage below 90%: {match.group(1)}%")
        else:
            passed.append(f"coverage>={match.group(1)}%")

    for path, label in (
        (performance_report, "performance profile"),
        (risk_fuzz_report, "risk-reduction fuzz"),
        (serialization_fuzz_report, "serialization fuzz"),
        (stress_report, "concurrency/soak stress"),
    ):
        try:
            report = _json(path)
            if label == "performance profile":
                if report.get("schema_version") != "1" or not report.get("cases"):
                    raise ValueError("performance profile lacks schema/cases")
            elif report.get("status") != "PASS":
                raise ValueError(f"{label} status is not PASS")
            passed.append(label)
        except ValueError as exc:
            failures.append(f"{label}: {exc}")

    tool_path = fast_ci_dir / f"tool-availability-{round_name}.txt"
    if tool_path.is_file():
        for line in _read(tool_path).splitlines():
            if line.endswith("=BLOCKED_TOOL_UNAVAILABLE"):
                blockers.append(line.replace("=BLOCKED_TOOL_UNAVAILABLE", ": unavailable"))
    else:
        failures.append(f"tool availability evidence is missing: {tool_path}")

    if not verified_checkout:
        blockers.append("source provenance: verified integration-branch checkout unavailable")

    external_checks = (
        (mac_heavy_audit_report, "Mac ARM64 Heavy Audit"),
        (strict_type_report, "strict MyPy/Pyright"),
        (external_security_report, "Bandit-equivalent security audit"),
        (dependency_audit_report, "dependency vulnerability audit"),
        (mutation_report, "full mutation testing"),
    )
    external_passed = True
    for path, label in external_checks:
        try:
            ok, detail = _external_report(path, label=label)
        except ValueError as exc:
            ok, detail = False, f"{label}: {exc}"
        if ok:
            passed.append(detail)
        else:
            external_passed = False
            blockers.append(detail)

    if failures:
        status = LOCAL_DEVELOPMENT
    elif verified_checkout and external_passed and not blockers:
        status = LOCAL_RELEASE_CANDIDATE
    else:
        status = LOCAL_VALIDATION

    return {
        "schema_version": "1",
        "status": status,
        "passed": sorted(set(passed)),
        "blockers": sorted(set(blockers)),
        "failures": sorted(set(failures)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast-ci-dir", type=Path, required=True)
    parser.add_argument("--round-name", required=True)
    parser.add_argument("--performance-report", type=Path, required=True)
    parser.add_argument("--risk-fuzz-report", type=Path, required=True)
    parser.add_argument("--serialization-fuzz-report", type=Path, required=True)
    parser.add_argument("--stress-report", type=Path, required=True)
    parser.add_argument("--verified-checkout", action="store_true")
    parser.add_argument("--mac-heavy-audit-report", type=Path)
    parser.add_argument("--strict-type-report", type=Path)
    parser.add_argument("--external-security-report", type=Path)
    parser.add_argument("--dependency-audit-report", type=Path)
    parser.add_argument("--mutation-report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_release_gate(
        fast_ci_dir=args.fast_ci_dir,
        round_name=args.round_name,
        performance_report=args.performance_report,
        risk_fuzz_report=args.risk_fuzz_report,
        serialization_fuzz_report=args.serialization_fuzz_report,
        stress_report=args.stress_report,
        verified_checkout=args.verified_checkout,
        mac_heavy_audit_report=args.mac_heavy_audit_report,
        strict_type_report=args.strict_type_report,
        external_security_report=args.external_security_report,
        dependency_audit_report=args.dependency_audit_report,
        mutation_report=args.mutation_report,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
