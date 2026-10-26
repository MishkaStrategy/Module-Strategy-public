from __future__ import annotations

from pathlib import Path

import pytest

import modular_results_registry.core_adapter as core_adapter
import modular_results_registry.core_adapter_io as core_io
from modular_results_registry.artifact_store import FileLimits
from modular_results_registry.errors import IntegrityError, ResourceLimitError


def test_load_json_object_rejects_resource_limit_with_contract_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "oversized.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(core_adapter, "_MAX_JSON_BYTES", 17)

    def fail(*args, **kwargs):
        raise ResourceLimitError("bounded read")

    monkeypatch.setattr(core_io, "read_verified_bytes", fail)
    with pytest.raises(ResourceLimitError, match="exceeds 17 bytes"):
        core_io._load_json_object(source, limits=FileLimits(max_file_bytes=100))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "invalid JSON artifact"),
        (b'{"broken":', "invalid JSON artifact"),
        (b"[]", "must contain a JSON object"),
        (b"null", "must contain a JSON object"),
    ],
)
def test_load_json_object_rejects_invalid_documents(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(payload)
    with pytest.raises(IntegrityError, match=message):
        core_io._load_json_object(source, limits=FileLimits())


def test_load_json_object_rejects_nonfinite_constant(tmp_path: Path) -> None:
    source = tmp_path / "metrics.json"
    source.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(IntegrityError, match="non-finite JSON constant"):
        core_io._load_json_object(source, limits=FileLimits())


def test_core_io_scalar_validators_are_fail_closed() -> None:
    assert core_io._require_mapping({"a": 1}, "mapping") == {"a": 1}
    with pytest.raises(IntegrityError, match="mapping must be a JSON object"):
        core_io._require_mapping([], "mapping")

    assert core_io._require_string("value", "field") == "value"
    for value in (None, "", 1, False):
        with pytest.raises(IntegrityError, match="non-empty string"):
            core_io._require_string(value, "field")

    assert core_io._require_int(0, "count") == 0
    assert core_io._require_int(7, "count") == 7
    for value in (True, -1, 1.5, "1", None):
        with pytest.raises(IntegrityError, match="non-negative integer"):
            core_io._require_int(value, "count")
