from __future__ import annotations

import errno

from market_data_ingestion.errors import StorageError
from market_data_ingestion.storage import LocalStorage

_PRESSURE_CASES: list[tuple[int, str, bool]] = [
    (errno.ENOSPC, "DISK_SPACE_EXHAUSTED", True),
    (errno.EROFS, "STORAGE_PERMISSION_DENIED", False),
    (errno.EACCES, "STORAGE_PERMISSION_DENIED", False),
    (errno.EPERM, "STORAGE_PERMISSION_DENIED", False),
]
if hasattr(errno, "EDQUOT"):
    _PRESSURE_CASES.insert(1, (errno.EDQUOT, "DISK_SPACE_EXHAUSTED", True))


def _failing_replace(error_number: int):
    def fail_replace(source: object, target: object) -> None:
        del source, target
        raise OSError(error_number, "simulated storage pressure")

    return fail_replace


def test_manifest_atomic_publication_classifies_storage_pressure_and_cleans_temp(
    tmp_path,
    monkeypatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    destination = storage.root / "manifests" / "pressure-case" / "v1.json"

    for error_number, expected_code, expected_retryable in _PRESSURE_CASES:
        monkeypatch.setattr(
            "market_data_ingestion.storage.os.replace",
            _failing_replace(error_number),
        )

        try:
            storage.write_manifest("pressure-case", 1, {"value": 1})
        except StorageError as error:
            assert error.code == expected_code
            assert error.retryable is expected_retryable
            assert error.stage == "storage"
            assert error.details["errno"] == error_number
            assert error.details["operation"] == "publishing an atomic artifact"
            assert error.details["path"] == str(destination)
        else:
            raise AssertionError("storage pressure must fail closed")

        assert not destination.exists()
        assert not tuple(destination.parent.glob(".*.tmp"))
