from __future__ import annotations

import importlib.util
from pathlib import Path

PROCESS_KILL_TEST = "test_process_kill_fault_recovery.py"
PROCESS_KILL_PACKAGES = (
    "modular_core",
    "modular_observability",
    "modular_risk_supervisor",
    "modular_trading_runner",
)


def pytest_ignore_collect(collection_path: Path) -> bool:
    if collection_path.name != PROCESS_KILL_TEST:
        return False
    return any(importlib.util.find_spec(name) is None for name in PROCESS_KILL_PACKAGES)
