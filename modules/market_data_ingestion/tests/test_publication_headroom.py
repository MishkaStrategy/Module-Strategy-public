from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publication_headroom.py"
_NAMESPACE = runpy.run_path(str(_SCRIPT), run_name="publication_headroom_test_module")
_monthly_batches = _NAMESPACE["_monthly_batches"]
_registry_integrity = _NAMESPACE["_registry_integrity"]
measure_publication = _NAMESPACE["measure_publication"]
_sqlite3 = _NAMESPACE["sqlite3"]


class _IntegrityCursor:
    def fetchone(self) -> tuple[str]:
        return ("ok",)


class _TrackedConnection:
    def __init__(self) -> None:
        self.closed = False

    def execute(self, statement: str) -> _IntegrityCursor:
        assert statement == "PRAGMA integrity_check"
        return _IntegrityCursor()

    def close(self) -> None:
        self.closed = True


class _FailingTrackedConnection(_TrackedConnection):
    def execute(self, statement: str) -> _IntegrityCursor:
        assert statement == "PRAGMA integrity_check"
        raise RuntimeError("integrity check failed")


def test_monthly_batches_preserve_scale_and_month_boundaries() -> None:
    batches = tuple(_monthly_batches(10_000))

    assert sum(len(batch) for batch in batches) == 10_000
    assert len(batches) == 2
    for batch in batches:
        assert batch
        months = {(record.timestamp.year, record.timestamp.month) for record in batch}
        assert len(months) == 1


def test_registry_integrity_closes_owned_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _TrackedConnection()
    monkeypatch.setattr(_sqlite3, "connect", lambda _database: connection)

    assert _registry_integrity(Path("registry.sqlite3")) == "ok"
    assert connection.closed is True


def test_registry_integrity_closes_owned_connection_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FailingTrackedConnection()
    monkeypatch.setattr(_sqlite3, "connect", lambda _database: connection)

    with pytest.raises(RuntimeError, match="integrity check failed"):
        _registry_integrity(Path("registry.sqlite3"))
    assert connection.closed is True


def test_measure_publication_smoke_is_sanitized_and_reopen_safe(tmp_path: Path) -> None:
    measurement = measure_publication(rows=100, root=tmp_path)
    payload = measurement.to_dict()
    encoded = json.dumps(payload, sort_keys=True)

    assert measurement.rows == 100
    assert measurement.partition_count == 1
    assert measurement.final_storage_bytes > 0
    assert measurement.peak_storage_bytes >= measurement.final_storage_bytes
    assert measurement.largest_atomic_payload_bytes >= measurement.largest_partition_bytes
    assert measurement.largest_partition_bytes > 0
    assert measurement.manifest_bytes > 0
    assert measurement.registry_bytes_after >= measurement.registry_bytes_before
    assert measurement.registry_growth_bytes >= 0
    assert measurement.registry_integrity == "ok"
    assert measurement.descriptor_reopen_match is True
    assert str(tmp_path) not in encoded
