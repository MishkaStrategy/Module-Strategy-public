from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.benchmark_dataset_storage_recovery import run_recovery_benchmark


def test_dataset_storage_recovery_benchmark_smoke() -> None:
    result = run_recovery_benchmark(manifest_count=20, orphan_count=20)
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    output = reports / (
        f"dataset-storage-recovery-py{sys.version_info.major}{sys.version_info.minor}.json"
    )
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    assert result["registry"]["integrity_check_result"] == "ok"
    assert result["registry"]["schema_version"] == 1
    assert result["registry"]["crash_recovery_ms"] >= 0
    assert result["local_storage"]["manifest_count"] == 20
    assert result["local_storage"]["removed_orphan_count"] == 20
    assert result["local_storage"]["temporary_bytes_before"] > 0
    assert result["local_storage"]["temporary_bytes_after"] == 0
    assert result["local_storage"]["reopen_ms"] >= 0
