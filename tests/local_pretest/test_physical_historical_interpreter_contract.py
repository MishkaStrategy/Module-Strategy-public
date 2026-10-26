from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_physical_mac_historical_acceptance.sh"


def test_physical_historical_script_has_valid_shell_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(SCRIPT)], cwd=ROOT, check=True)


def test_event_storage_evidence_uses_canonical_local_test_python() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    full_index = text.index("run_canonical full")
    interpreter_assignment = 'LOCAL_TEST_PYTHON="$GITHUB_WORKSPACE/.venv-local-test/bin/python"'
    interpreter_index = text.index(interpreter_assignment)
    result_hashing_index = text.index('CURRENT_STAGE="result_hashing"')

    assert full_index < interpreter_index < result_hashing_index
    assert (
        '[ -x "$LOCAL_TEST_PYTHON" ] '
        '|| die "canonical local-test Python missing after full suite"'
        in text[interpreter_index:result_hashing_index]
    )

    result_hashing = text[result_hashing_index:]
    result_hashing = result_hashing.split(
        'RESULTS_DIR="$GITHUB_WORKSPACE/.local-tests/pretest/results"', 1
    )[0]
    assert '"$LOCAL_TEST_PYTHON" scripts/write_physical_event_storage_evidence.py' in result_hashing
    assert "python scripts/write_physical_event_storage_evidence.py" not in result_hashing
