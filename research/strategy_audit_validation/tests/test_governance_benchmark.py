from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_benchmark(tmp_path: Path, *, suffix: str) -> dict[str, object]:
    package_root = Path(__file__).resolve().parents[1]
    output = tmp_path / f"result-{suffix}.json"
    database = tmp_path / f"ledger-{suffix}.sqlite3"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(package_root / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(package_root / "benchmarks" / "strategy_governance_slo.py"),
            "--workload",
            "valid",
            "--candidates",
            "3",
            "--database",
            str(database),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_benchmark_smoke_and_deterministic_digest(tmp_path: Path) -> None:
    first = _run_benchmark(tmp_path, suffix="first")
    second = _run_benchmark(tmp_path, suffix="second")
    assert first["workload"]["network_used"] is False  # type: ignore[index]
    assert first["workload"]["live_trading_used"] is False  # type: ignore[index]
    assert first["metrics"]["failure_counts"] == {}  # type: ignore[index]
    assert first["metrics"]["durable_counts"] == {  # type: ignore[index]
        "approvals": 3,
        "candidates": 3,
        "checkpoints": 3,
        "evidence": 3,
    }
    assert (
        first["determinism"]["logical_digest"]
        == (  # type: ignore[index]
            second["determinism"]["logical_digest"]  # type: ignore[index]
        )
    )
