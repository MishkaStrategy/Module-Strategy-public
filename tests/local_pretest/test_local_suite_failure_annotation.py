from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_local_test_suite.sh"
EXPECTED_SUBSTAGES = (
    "bootstrap",
    "install_dependencies",
    "local_pretest",
    "smoke_reset",
    "smoke_commands",
    "full_reset",
    "full_backtest",
)


def test_local_suite_failure_annotation_is_static_and_allowlisted() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "set -Eeuo pipefail" in text
    assert "trap report_failure_substage EXIT" in text
    assert "title=Canonical local suite failure" in text
    assert "canonical_local_failure mode=%s substage=%s exit_code=%d" in text
    for substage in EXPECTED_SUBSTAGES:
        assert substage in text


def test_full_corpus_uses_bounded_research_fast_observability() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    before_full, full_section = text.split('if [[ "$MODE" == "full" ]]; then', 1)
    full_section = full_section.split("\nfi\n", 1)[0]

    assert "--mode research_fast" in full_section
    assert "--max-events 125000000" in full_section
    assert "--max-skip-ranges 500000" in full_section
    assert "--mode research_fast" not in before_full
    assert "--max-events" not in before_full
    assert "--max-skip-ranges" not in before_full
    assert not re.search(r"--max-[a-z-]+\s+0(?:\s|$)", full_section)


def test_local_suite_bootstrap_failure_emits_bounded_safe_annotation(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    env = os.environ.copy()
    env["PATH"] = str(tmp_path / "empty-path")

    completed = subprocess.run(
        [bash, str(SCRIPT), str(tmp_path / "dataset"), "prepare"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode != 0
    annotations = [line for line in completed.stdout.splitlines() if line.startswith("::error ")]
    assert len(annotations) == 1
    annotation = annotations[0]
    assert re.fullmatch(
        r"::error title=Canonical local suite failure::"
        r"canonical_local_failure mode=prepare substage=bootstrap exit_code=[1-9][0-9]*",
        annotation,
    )
    assert str(ROOT) not in annotation
    assert str(tmp_path) not in annotation
    assert "\n" not in annotation
    assert "\r" not in annotation
    assert len(annotation.encode("utf-8")) < 512


def test_invalid_mode_never_enters_diagnostic_contract(tmp_path: Path) -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "dataset"), "not-a-mode"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 2
    assert "canonical_local_failure" not in completed.stdout
    assert "canonical_local_failure" not in completed.stderr
