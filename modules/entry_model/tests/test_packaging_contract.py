from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


def test_manifest_keeps_source_distribution_reproducible() -> None:
    project = Path(__file__).resolve().parents[1]
    manifest = (project / "MANIFEST.in").read_text(encoding="utf-8")
    for directive in (
        "recursive-include tests *.py",
        "recursive-include benchmarks *.py",
        "recursive-include scripts *.py *.sh",
        "recursive-include examples *.py *.yaml",
        "recursive-include docs *.md",
    ):
        assert directive in manifest


def test_required_test_helpers_are_versioned_with_package() -> None:
    project = Path(__file__).resolve().parents[1]
    assert (project / "tests" / "__init__.py").is_file()
    assert (project / "tests" / "support.py").is_file()


def test_ci_python_resolver_accepts_the_active_supported_interpreter() -> None:
    project = Path(__file__).resolve().parents[1]
    resolver = project / "scripts" / "resolve_ci_python.sh"
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert version in {"3.12", "3.13"}

    env = os.environ.copy()
    env.pop("GITHUB_OUTPUT", None)
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", str(resolver), version],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    resolved = Path(result.stdout.strip().splitlines()[-1]).resolve()
    active = Path(sys.executable).resolve()
    assert resolved == active


def test_ci_python_resolver_rejects_unsupported_versions_without_installing() -> None:
    project = Path(__file__).resolve().parents[1]
    resolver = project / "scripts" / "resolve_ci_python.sh"
    result = subprocess.run(
        ["bash", str(resolver), "3.11"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_OUTPUT": ""},
    )
    assert result.returncode == 64
    assert "unsupported CI Python version" in result.stderr


def test_packaging_workflow_creates_reports_directory_before_checksums() -> None:
    project = Path(__file__).resolve().parents[1]
    workflow = (project.parents[1] / ".github" / "workflows" / "entry-model-ci.yml").read_text(
        encoding="utf-8"
    )
    checksum_step = workflow.split("      - name: Checksums", maxsplit=1)[1].split(
        "      - uses: actions/upload-artifact", maxsplit=1
    )[0]
    mkdir = "mkdir -p modules/entry_model/reports"
    output = "modules/entry_model/reports/distribution-checksums.sha256"
    assert mkdir in checksum_step
    assert output in checksum_step
    assert checksum_step.index(mkdir) < checksum_step.index(output)


def test_benchmark_rss_normalization_is_platform_aware() -> None:
    project = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(project / "benchmarks" / "benchmark_entry_model.py"))
    normalize = namespace["normalize_max_rss_kib"]

    assert normalize(75_595_776, system_name="Darwin") == 73_824
    assert normalize(73_824, system_name="Linux") == 73_824


def test_benchmark_rss_normalization_rejects_negative_values() -> None:
    project = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(project / "benchmarks" / "benchmark_entry_model.py"))
    normalize = namespace["normalize_max_rss_kib"]

    try:
        normalize(-1, system_name="Darwin")
    except ValueError as error:
        assert str(error) == "max RSS cannot be negative"
    else:
        raise AssertionError("negative max RSS must fail")
