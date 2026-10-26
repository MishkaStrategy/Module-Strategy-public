from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.authoritative_historical_parity import (
    HistoricalParityModule,
    _write_module_config,
)


def test_module_config_preserves_runtime_module_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(HistoricalParityModule, "__module__", "__main__")
    path = tmp_path / "modules.json"

    _write_module_config(path, "a" * 64)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["modules"][0]["class"] == "__main__:HistoricalParityModule"
