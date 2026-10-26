from __future__ import annotations

import platform
import socket
from pathlib import Path
from xml.etree import ElementTree

from scripts.paper_execution_component_gate import sanitize_junit_report


def _write_junit(path: Path, *, hostname: str, system_out: str = "") -> None:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        "<testsuites>"
        f'<testsuite name="pytest" tests="1" hostname="{hostname}">'
        '<testcase classname="tests.test_example" name="test_example" />'
        f"<system-out>{system_out}</system-out>"
        "</testsuite>"
        "</testsuites>",
        encoding="utf-8",
    )


def test_sanitize_junit_report_redacts_runner_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner_identity = "sensitive-" + "runner-1234"
    monkeypatch.setattr(platform, "node", lambda: runner_identity)
    monkeypatch.setattr(socket, "gethostname", lambda: runner_identity)
    report = tmp_path / "tests.xml"
    _write_junit(
        report,
        hostname=runner_identity,
        system_out=f"executed on {runner_identity}",
    )

    sanitize_junit_report(report)

    tree = ElementTree.parse(report)
    suite = next(tree.getroot().iter("testsuite"))
    payload = report.read_text(encoding="utf-8")
    assert suite.attrib["hostname"] == "redacted-runner"
    assert runner_identity not in payload
    assert "executed on redacted-runner" in payload


def test_sanitize_junit_report_redacts_private_paths_and_file_uris(tmp_path: Path) -> None:
    private_home = "/" + "Users" + "/evidence-" + "owner"
    private_uri = "file:" + "//" + private_home + "/workspace/report.txt"
    report = tmp_path / "tests.xml"
    _write_junit(
        report,
        hostname="already-public",
        system_out=f"home={private_home} report={private_uri}",
    )

    sanitize_junit_report(report)

    payload = report.read_text(encoding="utf-8")
    assert private_home not in payload
    assert private_uri not in payload
    assert "redacted-home" in payload
    assert "redacted-local-uri" in payload
